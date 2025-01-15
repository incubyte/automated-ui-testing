import json
import asyncio
from computer_use_demo.tools import ComputerTool, BashTool

async def run_commands(file_path):
    # Initialize tools
    computer = ComputerTool()
    bash = BashTool()
    
    # Kill Firefox if running and restart it
    await bash(command="pkill firefox-esr || true") 
    await asyncio.sleep(2)
    await bash(command="firefox-esr -new-window &")
    await asyncio.sleep(5)
    
    # Read and execute commands
    with open(file_path, 'r') as f:
        commands = json.load(f)
        
    for cmd in commands:
        if cmd["name"] == "computer":
            await computer(**cmd["input"])
            await asyncio.sleep(2)