"""Notion: tasks (quests), projects (campaigns), stats and pages.

Auth: an internal integration token (notion.so/profile/integrations) with the tasks
database shared to it. The database's columns are read from Notion, so any
"title + date + status/checkbox" database works, not only the "My Tasks" template.
Projects come for free when the tasks database has a relation to another database
(Hakim's Hub: Quests -> Campaigns); Notion's relation names that database, so no setting.

- schema.py: which column means what, and parsed rows
- client.py: the API
- tasks.py / projects.py: the tools, in the "notion" and "projects" router categories
- reports.py: plain-text lists and the proactive messages
"""
from .client import API, VERSION, Notion
from .projects import claim, find_project, parse_percent
from .projects import tools as project_tools
from .reports import (
                      NO_PROJECTS,
                      NOT_SET_UP,
                      morning_tasks,
                      overdue_nudge,
                      period,
                      projects_report,
                      stats_text,
                      tasks_report,
                      weekly_review,
)
from .schema import ProjectSchema, StatusColumn, Task, TaskSchema, database_id, label
from .tasks import best_matches, guess, rescue, task_words, tools

__all__ = ["API", "VERSION", "Notion", "claim", "find_project", "parse_percent", "project_tools", "NO_PROJECTS",
           "NOT_SET_UP", "morning_tasks", "overdue_nudge", "period", "projects_report", "stats_text",
           "tasks_report", "weekly_review", "ProjectSchema", "StatusColumn", "Task", "TaskSchema", "database_id",
           "label", "best_matches", "guess", "rescue", "task_words", "tools"]
