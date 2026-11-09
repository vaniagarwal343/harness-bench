#!/usr/bin/env bash
# One-time harness install at pinned commits, each in its own virtualenv. No API calls.
#   bash adapters/setup.sh deepagents | gptr | all
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p vendor
pin() {  # pin <dir> <url> <sha>
  [[ -d "vendor/$1/.git" ]] || git clone --quiet "$2" "vendor/$1"
  git -C "vendor/$1" fetch --quiet origin "$3" 2>/dev/null || git -C "vendor/$1" fetch --quiet origin
  git -C "vendor/$1" checkout --quiet "$3"
  echo "$1 @ $(git -C "vendor/$1" rev-parse HEAD)"
}
want="${1:-all}"
if [[ "$want" == deepagents || "$want" == all ]]; then
  pin deepagents https://github.com/langchain-ai/deepagents d315d8e12dd4f2c250a759a38825f1ea1c9484d6
  uv venv --quiet --python 3.12 .venv-deepagents
  uv pip install --quiet --python .venv-deepagents/bin/python ./vendor/deepagents/libs/deepagents langchain-anthropic langchain tavily-python markdownify httpx langchain-google-genai
  uv pip freeze --python .venv-deepagents/bin/python | sed -E "s#^deepagents @ file://.*#deepagents @ git+https://github.com/langchain-ai/deepagents@d315d8e12dd4f2c250a759a38825f1ea1c9484d6\#subdirectory=libs/deepagents#" > deepagents.lock.txt
fi
if [[ "$want" == gptr || "$want" == all ]]; then
  pin gpt-researcher https://github.com/assafelovic/gpt-researcher ddd4db36208d9c8e49f3b2436efda97baecb5433
  git -C vendor/gpt-researcher checkout --quiet -- . && git -C vendor/gpt-researcher apply ../../patches/gptr-anthropic-text.patch   # text view of thinking responses
  uv venv --quiet --python 3.12 .venv-gptr
  uv pip install --quiet --python .venv-gptr/bin/python ./vendor/gpt-researcher langchain-anthropic
  uv pip freeze --python .venv-gptr/bin/python | sed -E "s#^gpt-researcher @ file://.*#gpt-researcher @ git+https://github.com/assafelovic/gpt-researcher@ddd4db36208d9c8e49f3b2436efda97baecb5433#" > gptr.lock.txt
fi
