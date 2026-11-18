"""Synthetic full-stack fixture server. Never imported by the application."""
from datetime import timedelta
from pathlib import Path
import sys

import uvicorn

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'tests'))
from test_terminal_retirement import ready
from test_paper_reviews import ReadOnly
from test_paper_trading import NOW
from orb.dashboard_api import create_app

root=Path(sys.argv[1])
engine,path=ready(root)
engine.close()
app=create_app(port=8892,web_dir=Path(__file__).resolve().parents[1]/"build",paper_db=path,preset_db=root/'presets.sqlite',research_db=root/'research.sqlite',data_dir=root/'data')
app.state.paper_engine.client=ReadOnly()  # asserts GET; cannot submit/cancel
app.state.paper_engine.review_clock=lambda:NOW+timedelta(minutes=1)
app.state.paper_engine.evidence.clock=app.state.paper_engine.review_clock
uvicorn.run(app,host='127.0.0.1',port=8892,log_level='warning')
