#!/usr/bin/env bash
# 로컬 vLLM(OpenAI 호환, 8000). 사용: bash wsl_serve.sh Qwen/Qwen2.5-7B-Instruct-AWQ
# wsl.exe가 살아 있어야 서버도 산다(nohup으로 띄우면 wsl.exe 종료와 함께 죽는다).
set -e
source ~/vllm-venv/bin/activate
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
export VLLM_WSL2_ENABLE_PIN_MEMORY=1 VLLM_USE_FLASHINFER_SAMPLER=0
MODEL="$1"; shift
exec vllm serve "$MODEL" --port 8000 --max-model-len 8192 --gpu-memory-utilization 0.85 "$@"
