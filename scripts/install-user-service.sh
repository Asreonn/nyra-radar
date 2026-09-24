#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
systemctl --user link "$project_dir/systemd/nyra-radar-api.service"
systemctl --user link "$project_dir/systemd/nyra-radar-worker.service"
systemctl --user link "$project_dir/systemd/nyra-radar.target"
systemctl --user daemon-reload
systemctl --user enable --now nyra-radar.target
systemctl --user --no-pager status nyra-radar-api.service nyra-radar-worker.service
