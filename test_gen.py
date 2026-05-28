import asyncio
import os
from dotenv import load_dotenv
from scenario_generator import generate_scenario

async def test():
    load_dotenv()
    try:
        scenario = await generate_scenario("I am a space pirate trying to buy a map to a hidden treasure in a French-speaking planet.")
        print("SUCCESS")
        print(scenario)
    except Exception as e:
        print(f"FAILURE: {e}")

if __name__ == "__main__":
    asyncio.run(test())
