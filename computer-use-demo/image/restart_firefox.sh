#!/bin/bash

set -e

export DISPLAY=:${DISPLAY_NUM}

pkill -f firefox-esr || true

# Launch Firefox in the background after display setup
(sleep 2 && firefox-esr -new-window &)