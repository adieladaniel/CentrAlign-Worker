"""`python -m company_sim` starts the simulator on port 8001."""
import uvicorn

from . import config

if __name__ == "__main__":
    uvicorn.run("company_sim.app:app", host=config.HOST, port=config.PORT, log_level="info")
