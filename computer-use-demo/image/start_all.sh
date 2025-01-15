#!/bin/bash

set -e

export DISPLAY=:${DISPLAY_NUM}
./xvfb_startup.sh
./tint2_startup.sh
./mutter_startup.sh
./x11vnc_startup.sh

# Launch Firefox in the background after display setup
(sleep 2 && firefox-esr -new-window &)