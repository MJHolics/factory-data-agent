#!/usr/bin/env bash
# 비교용 SQL 특화 모델을 로컬 캐시에 받는다(한 번만). 이후 서빙은 HF_HUB_OFFLINE=1.
set -e
source ~/vllm-venv/bin/activate
python - <<'PY'
from huggingface_hub import snapshot_download
for repo in ["Qwen/Qwen2.5-Coder-7B-Instruct-AWQ"]:
    p = snapshot_download(repo)
    print("ok", repo, p)
PY
