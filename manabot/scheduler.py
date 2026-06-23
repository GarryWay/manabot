from manabot.config import Config


def schedule_run(cron_expression: str, config: Config) -> None:
    raise NotImplementedError(
        f"Scheduled runs are not yet implemented. "
        f"To enable: add apscheduler>=3.10 to dependencies and implement this function. "
        f"The fetch_runs table in the DB tracks the last run time for scheduling decisions."
    )
