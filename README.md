# API-Linkging-Bridge
A lightweight API forwarding proxy bridge. It listens on a custom local port, automatically handles request escaping and protocol adaptation, forwards traffic to upstream API providers, and relays responses back to local clients.

✅ Fix 404 errors when scanning model lists

✅ Avoid timeout issues for long conversations and large requests

✅ Dual versions: GUI desktop & pure terminal CLI

✅ Lightweight with no heavy dependencies, ready to use out of the box

# Usage Guide

## GUI Version

1. Double-click to launch the program

2. Go to **Link Management** tab, configure link name, Base URL, model name, API Key, port, conversion mode and other parameters as needed

3. Click the **Test Connection** button

4. Switch to **Console** tab, select a link and start it

## CLI Version
1. Run the program by double-clicking, or launch it from terminal

2. Enter the `add` command to create a new link, follow the prompts to finish configuration

3. After configuration, use the **Up / Down arrow keys** to move the highlight selector for link selection

4. Once selected, enter `start` to activate the link, or `stop` to halt it

5. Enter `delete` to remove the selected link. **Double-check your selection before deletion to prevent accidental data loss**

6. Use `stats` to view link status information, and `help` to list all available commands and usages

# ⚠️ Note: Verify that your target port is not occupied by other programs before starting the link, otherwise the service will fail to run!

This project is fully open-source and allows secondary development.

For more information:

https://ctxcode.top - (Homepage)

https://ctxcode.cn - (Homepage)

https://alb.ctxcode.top - (Project intro & download page)

https://alb.ctxcode.cn - (Project intro & download page)
