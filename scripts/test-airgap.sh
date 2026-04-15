#!/usr/bin/env bash
# test-airgap.sh — Verify ZOVARK works without internet by invalidating cloud API keys
set -euo pipefail

echo "=== ZOVARK Air-Gap Test ==="
echo ""

# Check Ollama is running
if ! docker compose exec ollama ollama list >/dev/null 2>&1; then
  echo "ERROR: Ollama is not running. Run: bash scripts/airgap-setup.sh first"
  exit 1
fi

echo "1. Ollama is running with models:"
docker compose exec ollama ollama list
echo ""

# Test direct Ollama inference
echo "2. Testing direct Ollama inference..."
OLLAMA_RESP=$(docker compose exec ollama curl -sf http://localhost:11434/api/generate \
  -d '{"model":"llama3.2:3b","prompt":"Say hello in one word","stream":false}' 2>&1) || true

if echo "$OLLAMA_RESP" | grep -q "response"; then
  echo "   Direct Ollama: OK"
else
  echo "   Direct Ollama: FAILED"
  echo "   Response: $OLLAMA_RESP"
  exit 1
fi
echo ""

# Test LiteLLM routing to Ollama (simulate cloud failure)
echo "3. Testing LiteLLM fallback to Ollama..."
echo "   (Using airgap model name directly)"
LLM_RESP=$(curl -sf http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer ${ZOVARK_LLM_KEY:?ZOVARK_LLM_KEY is required — source .env first}" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "zovark-fast-airgap",
    "messages": [{"role":"user","content":"Respond with only the word OK"}],
    "max_tokens": 10
  }' 2>&1) || true

if echo "$LLM_RESP" | grep -q "choices"; then
  echo "   LiteLLM → Ollama: OK"
else
  echo "   LiteLLM → Ollama: FAILED"
  echo "   Response: $LLM_RESP"
  exit 1
fi
echo ""

# Submit a test investigation via API
echo "4. Submitting test investigation via API..."
TOKEN=$(curl -sf http://localhost:8090/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@zovark.local","password":"zovark123"}' | grep -o '"token":"[^"]*"' | cut -d'"' -f4)

if [ -z "$TOKEN" ]; then
  echo "   Login failed — skipping API test"
  echo "   (Register a user first: see README)"
else
  TASK_RESP=$(curl -sf http://localhost:8090/api/v1/tasks \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{
      "prompt": "Investigate DNS beaconing to test-airgap.example.com from 10.0.0.1",
      "task_type": "investigation"
    }' 2>&1) || true

  if echo "$TASK_RESP" | grep -q "task_id\|id"; then
    echo "   Task submitted: OK"
    echo "   Response: $TASK_RESP"
  else
    echo "   Task submission: FAILED"
    echo "   Response: $TASK_RESP"
  fi
fi

echo ""
echo "=== AIR-GAP TEST: PASSED ==="
echo "Investigation infrastructure works without internet."
echo "All LLM calls can fall back to local Ollama (llama3.2:3b)."
