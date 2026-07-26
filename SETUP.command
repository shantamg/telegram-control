#!/bin/zsh

set -e
cd "${0:A:h}"

./telegram_bridge.py setup
./telegram_bridge.py send "✅ Telegram bridge paired with this Mac."

echo
echo "Proof of concept is ready."
echo "Send any message to your new bot. The Mac will acknowledge it."
echo "Keep this window open for this test; press Control-C when finished."
echo

exec ./telegram_bridge.py listen
