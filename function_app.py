"""Azure Functions entry point: the daily pipeline, on a timer.

Every value the scripts need comes from the app settings, with secrets as
Key Vault references; there is no .env in Azure.
"""
import logging
import pathlib
import sys

import azure.functions as func

# The pipeline's modules live in python/; make them importable here.
sys.path.insert(0, str(pathlib.Path(__file__).parent / "python"))

app = func.FunctionApp()


# Schedules are in UTC. 23:30 UTC is 10:00 in Adelaide during daylight
# saving (October to April) and 9:00 the rest of the year.
@app.timer_trigger(schedule="0 30 23 * * *", arg_name="timer",
                   run_on_startup=False, use_monitor=True)
def daily_pipeline(timer: func.TimerRequest) -> None:
    import pipeline
    failures = pipeline.run_and_report(["azure"], log=logging.info)
    if failures:
        # Marks the run as failed in the function app's logs
        raise RuntimeError("Pipeline steps failed: "
                           + ", ".join(script for script, _ in failures))