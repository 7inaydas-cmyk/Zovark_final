"""
Pydantic validation schemas for LLM outputs.

Validates verdicts, IOCs, MITRE technique IDs, and tool selection.
Invalid data is caught gracefully — investigations never crash from validation.
"""
import re
import logging
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)

# Audit 2.24: accept both techniques (T1234, T1234.001) and tactic IDs (TA0001–TA0043).
# Previously dropped every TA#### silently at validate_mitre_ids.
_MITRE_PATTERN = re.compile(r"^T(?:A\d{4}|\d{4}(?:\.\d{3})?)$")


class IOCItem(BaseModel):
    """Validates an Indicator of Compromise."""
    # Audit 2.25: accept both "ioc_type" and "type" keys. Every pipeline producer
    # writes "type", so strict validation would silently drop every IOC.
    model_config = ConfigDict(populate_by_name=True)

    ioc_type: Literal[
        "ip", "domain", "url", "hash_md5", "hash_sha1", "hash_sha256",
        "email", "username", "cve",
    ] = Field(alias="type")
    value: str = Field(min_length=1)
    context: str = ""

    @field_validator("value")
    @classmethod
    def validate_ioc_value(cls, v, info):
        ioc_type = info.data.get("ioc_type")
        if ioc_type == "cve" and not re.match(r"^CVE-\d{4}-\d{4,}$", v):
            raise ValueError(f"Invalid CVE format: {v}")
        if ioc_type == "hash_md5" and len(v) != 32:
            raise ValueError(f"MD5 hash must be 32 chars, got {len(v)}")
        if ioc_type == "hash_sha1" and len(v) != 40:
            raise ValueError(f"SHA1 hash must be 40 chars, got {len(v)}")
        if ioc_type == "hash_sha256" and len(v) != 64:
            raise ValueError(f"SHA256 hash must be 64 chars, got {len(v)}")
        if ioc_type == "domain" and " " in v:
            raise ValueError(f"Domain contains spaces: {v}")
        return v


class VerdictOutput(BaseModel):
    """Validates the final investigation verdict.

    Uses the EXISTING verdict terms from the pipeline:
    true_positive, suspicious, benign, inconclusive, error.
    Does NOT include terms like mal1cious or conf1dence.
    """
    verdict: Literal["true_positive", "suspicious", "needs_review", "benign", "inconclusive", "error"]
    risk_score: int = Field(ge=0, le=100)
    severity: Literal["critical", "high", "medium", "low", "info"]
    summary: str = Field(min_length=1, max_length=5000)
    mitre_techniques: list[str] = []
    iocs: list[IOCItem] = []
    evidence_chain: list[str] = []

    @field_validator("mitre_techniques")
    @classmethod
    def validate_mitre_ids(cls, v):
        valid = []
        for technique in v:
            if _MITRE_PATTERN.match(technique):
                valid.append(technique)
            else:
                logger.warning(f"Dropped invalid MITRE ID: {technique}")
        return valid


class ToolSelectionOutput(BaseModel):
    """Validates LLM tool selection (Path C)."""
    tools: list[str]
    reasoning: str = ""

    @field_validator("tools")
    @classmethod
    def validate_tool_names(cls, v):
        try:
            from tools.catalog import TOOL_CATALOG
            valid = [t for t in v if t in TOOL_CATALOG]
        except ImportError:
            valid = v  # Can't validate without catalog
        if not valid:
            raise ValueError("No valid tools selected by LLM")
        if len(valid) < len(v):
            dropped = [t for t in v if t not in valid]
            logger.warning(f"Dropped invalid tools: {dropped}")
        return valid
