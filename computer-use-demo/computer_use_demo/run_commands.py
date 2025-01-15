import json
import asyncio
from computer_use_demo.tools import ComputerTool, BashTool
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def run_commands(commands):
    # Initialize tools
    computer = ComputerTool()
    
    # execute commands        
    logger.info(f"Read Commands: {commands}")

    for cmd in commands:
        if cmd["name"] == "computer":
            logger.info(f"Running computer command: {cmd['input']}")
            await computer(**cmd["input"])
            await asyncio.sleep(2)