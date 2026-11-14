"""Explicit L1/L2 setup fixtures. Never use these records for a real series.

Legacy suites call this helper instead of creating an unconfirmed project. The
choices below are test inputs; they are not evidence of a human interaction.
"""
from src.core.storage import init_project_layout as _init_project_layout
from src.core.series_setup import record_series_setup
from unit.test_series_setup import _make_valid_answers_dict


def init_confirmed_project(slug, *args, **kwargs):
    project = _init_project_layout(slug, *args, **kwargs)
    answers = _make_valid_answers_dict()
    for key, value in dict(Q02='local', Q05=False, Q07=False,
                           Q08={'no_upload': True}, Q09='final', Q10=False).items():
        answers[key]['user_answer'] = value
    record_series_setup(slug, answers, project_dir=project)
    return project
