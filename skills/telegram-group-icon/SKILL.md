---
name: telegram-group-icon
description: Create, choose, and set the profile photo, avatar, or icon of the Telegram group that owns the current Telegram Control-managed Codex or Claude turn. Use implicitly whenever the user asks to make, update, replace, or apply a group icon or group photo in the current Telegram group. The user does not need to mention this skill by name.
---

# Telegram Group Icon

Operate only on the group that owns the active managed turn. The scoped helper
does not accept a chat ID and refuses private chats.

1. Resolve the image:
   - If the user identifies an existing repo asset, inspect it and use the
     canonical square icon or logo mark.
   - If the user asks for a new icon and image generation is available, create
     a square image with the subject centered inside the middle 70 percent so
     Telegram's circular crop remains clear.
   - If no suitable image exists and image generation is unavailable, ask the
     user to attach or provide an image.
2. Inspect the final local image. Use a PNG or JPEG no larger than 10 MB.
3. Apply it to the current group:

```bash
/usr/bin/python3 /Users/shantam/telegram-control/agent_telegram.py \
  group-icon --image /absolute/path/to/icon.png
```

Use this only inside a Telegram Control-managed turn. Do not work around a
permission failure, accept a user-supplied chat ID, or modify another group.
Report success only after the helper succeeds.
