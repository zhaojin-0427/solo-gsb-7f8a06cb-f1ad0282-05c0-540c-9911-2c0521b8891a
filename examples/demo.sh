#!/usr/bin/env bash
# End-to-end demonstration against a running server:
# create session -> pull/ack all pages -> switch -> target-boot reboot report.
#
# Requires: curl, python3. Override BASE_URL / DEVICE_ID via env vars.
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8080}"
DEVICE_ID="${DEVICE_ID:-demo-device}"
ACTIVE_PARTITION="${ACTIVE_PARTITION:-a}"
ACTIVE_DIGEST="${ACTIVE_DIGEST:-$(printf 'old-firmware-image' | sha256sum | cut -d' ' -f1)}"

H=(-H "Content-Type: application/json" -H "X-Device-Id: ${DEVICE_ID}")

b64()  { printf '%s' "$1" | base64 | tr -d '\n'; }
sha()  { printf '%s' "$1" | sha256sum | cut -d' ' -f1; }

P0=$(printf 'firmware-page-0')
P1=$(printf 'firmware-page-1')
B0=$(b64 "$P0"); B1=$(b64 "$P1")
D0=$(sha "$P0"); D1=$(sha "$P1")

echo "== create session (active=${ACTIVE_PARTITION}) =="
SESSION_JSON=$(curl -sS "${H[@]}" -X POST "${BASE_URL}/sessions" -d "{
  \"device_id\": \"${DEVICE_ID}\",
  \"active_partition\": \"${ACTIVE_PARTITION}\",
  \"active_image_digest\": \"${ACTIVE_DIGEST}\",
  \"pages\": [
    {\"data\": \"${B0}\", \"sha256\": \"${D0}\"},
    {\"data\": \"${B1}\", \"sha256\": \"${D1}\"}
  ]
}")
echo "${SESSION_JSON}" | python3 -m json.tool
SID=$(printf '%s' "${SESSION_JSON}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
TARGET=$(printf '%s' "${SESSION_JSON}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["target_image_digest"])')

ack_pages() {
  while true; do
    INST=$(curl -sS "${H[@]}" -X POST "${BASE_URL}/devices/me/instructions/pull")
    KIND=$(printf '%s' "${INST}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["kind"])')
    [ "${KIND}" = "switch" ] && { printf '%s' "${INST}"; break; }

    IDX=$(printf '%s' "${INST}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["page_index"])')
    IID=$(printf '%s' "${INST}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
    PART=$(printf '%s' "${INST}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["payload"]["partition"])')
    echo "== write page ${IDX} to partition ${PART} (instruction ${IID}) ==" >&2
    DIGEST=$([ "${IDX}" = "0" ] && printf '%s' "${D0}" || printf '%s' "${D1}")
    curl -sS "${H[@]}" -X POST \
      "${BASE_URL}/devices/me/instructions/${IID}/page-report" \
      -d "{\"sha256\": \"${DIGEST}\"}" | python3 -m json.tool >&2
  done
}

echo "== processing writes =="
SW=$(ack_pages)
SW_ID=$(printf '%s' "${SW}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
echo "== switch instruction: ${SW_ID} =="
echo "${SW}" | python3 -m json.tool

echo "== reboot into target partition =="
TARGET_PARTITION=$([ "${ACTIVE_PARTITION}" = "a" ] && echo b || echo a)
curl -sS "${H[@]}" -X POST "${BASE_URL}/devices/me/reboot-reports" -d "{
  \"report_id\": \"demo-report-1\",
  \"switch_instruction_id\": \"${SW_ID}\",
  \"active_partition\": \"${TARGET_PARTITION}\",
  \"active_image_digest\": \"${TARGET}\"
}" | python3 -m json.tool

echo "== final session state =="
curl -sS "${H[@]}" "${BASE_URL}/devices/me/sessions/${SID}" | python3 -m json.tool

echo "== instruction history =="
curl -sS "${H[@]}" "${BASE_URL}/devices/me/sessions/${SID}/instructions" \
  | python3 -m json.tool
