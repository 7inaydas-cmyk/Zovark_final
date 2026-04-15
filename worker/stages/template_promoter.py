"""
Template Promoter — converts Path C generated code into deterministic skill templates.
Called when an analyst confirms a Path C investigation and requests promotion.
"""
import hashlib
import logging
import os
import re
from typing import Optional, Tuple

import psycopg2

logger = logging.getLogger(__name__)
# stabilize-runtime-hygiene: centralized DB URL via settings.
from settings import settings as _settings
DATABASE_URL = os.environ.get("DATABASE_URL", _settings.database_url)


def generate_template_slug(task_type: str, task_id: str) -> str:
    """Generate a unique slug for an auto-promoted template."""
    short_hash = hashlib.sha256(task_id.encode()).hexdigest()[:6]
    clean_type = re.sub(r'[^a-z0-9]', '-', task_type.lower())
    return f"auto-{clean_type}-{short_hash}"


def templatize_code(code: str, siem_event: dict) -> str:
    """Replace hardcoded values in generated code with {{siem_event_json}} placeholder."""
    import json
    template = code

    # Replace the JSON.loads call with the template placeholder
    siem_json = json.dumps(siem_event, indent=2)
    if siem_json in template:
        template = template.replace(siem_json, '{{siem_event_json}}')

    # Replace common hardcoded patterns from the SIEM event
    for key in ['source_ip', 'src_ip', 'username', 'user', 'hostname', 'rule_name']:
        val = siem_event.get(key, '')
        if val and len(val) > 3 and val in template:
            template = template.replace(f'"{val}"', f'siem_event.get("{key}", "")')
            template = template.replace(f"'{val}'", f"siem_event.get('{key}', '')")

    # Ensure the template has the placeholder
    if '{{siem_event_json}}' not in template and 'siem_event_json' not in template:
        # Prepend a default SIEM event loading block
        template = 'import json\nsiem_event = json.loads("""{{siem_event_json}}""")\n\n' + template

    return template


def validate_template_code(code: str) -> Tuple[bool, str]:
    """Basic validation that the template code is syntactically valid."""
    import ast
    try:
        # Replace placeholder for AST parsing
        test_code = code.replace('{{siem_event_json}}', '{"test": true}')
        ast.parse(test_code)
        return True, "valid"
    except SyntaxError as e:
        return False, f"SyntaxError: {e}"


def promote_to_template(
    task_id: str,
    task_type: str,
    generated_code: str,
    siem_event: dict,
    tenant_id: str,
    analyst_email: str,
) -> Optional[str]:
    """
    Promote Path C generated code to a skill template.
    Returns the template slug on success, None on failure.
    """
    slug = generate_template_slug(task_type, task_id)
    template_code = templatize_code(generated_code, siem_event)

    is_valid, error = validate_template_code(template_code)
    if not is_valid:
        logger.error(f"Template validation failed for {slug}: {error}")
        return None

    try:
        conn = psycopg2.connect(DATABASE_URL)
        with conn:
            with conn.cursor() as cur:
                # Check if slug already exists
                cur.execute("SELECT id FROM agent_skills WHERE skill_slug = %s", (slug,))
                if cur.fetchone():
                    logger.info(f"Template {slug} already exists, skipping promotion")
                    return slug

                # Insert new skill template
                cur.execute("""
                    INSERT INTO agent_skills (
                        skill_name, skill_slug, task_types, code_template,
                        auto_promoted, source_task_id, promoted_at, promoted_by,
                        promotion_status, tenant_id
                    ) VALUES (
                        %s, %s, %s, %s,
                        true, %s, NOW(), %s,
                        'active', %s
                    )
                """, (
                    f"Auto: {task_type} investigation",
                    slug,
                    [task_type],
                    template_code,
                    task_id,
                    analyst_email,
                    tenant_id,
                ))
                logger.info(f"Template promoted: {slug} from task {task_id}")
        conn.close()
        return slug
    except Exception as e:
        logger.error(f"Template promotion failed: {e}")
        return None
