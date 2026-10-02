"""共享数据库 engine（main 与测量路由共用，测试可整体替换）。"""
from __future__ import annotations

from sqlalchemy import create_engine

from .config import DATABASE_URL

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
