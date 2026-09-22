"""Bounded account and synchronization payloads shared by HTTP and WebSocket handlers."""
import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


def email_address(value):
    value = value.strip().lower()
    if len(value) > 254 or not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63}", value):
        raise ValueError('请输入有效邮箱地址')
    return value


class Credentials(StrictModel):
    email: str = Field(max_length=254)
    password: str = Field(min_length=12, max_length=128)
    normalize_email = field_validator('email')(email_address)


class PasswordChange(StrictModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=12, max_length=128)


class DeviceLogin(Credentials):
    name: str = Field(min_length=1, max_length=80)
    device_id: Optional[str] = Field(default=None, pattern=r'^[A-Za-z0-9_-]{1,80}$')
    expected_account: Optional[str] = Field(default=None, pattern=r'^[0-9a-f]{32}$')


class SessionData(StrictModel):
    project: str = Field(min_length=1, max_length=80)
    agent: Literal['codex', 'claude']
    status: Literal['running', 'stopped'] = 'running'


class EventData(StrictModel):
    id: int = Field(ge=1, le=9007199254740991)
    source: Literal['user', 'interaction']
    text: str = Field(max_length=16000)
    created_at: float = Field(ge=0, le=253402300799, allow_inf_nan=False)
    status: Literal['pending', 'sent', 'uncertain']


class TerminalData(StrictModel):
    text: str = Field(max_length=8000)
    observed_at: float = Field(ge=0, le=253402300799, allow_inf_nan=False)


class ProjectData(StrictModel):
    id: str = Field(min_length=1, max_length=80)
    agents: list[Literal['codex', 'claude']] = Field(max_length=2)


class ProjectsData(StrictModel):
    projects: list[ProjectData] = Field(max_length=100)


class SyncEntry(StrictModel):
    seq: int = Field(ge=1, le=9007199254740991)
    kind: Literal['session', 'event', 'terminal', 'deleted', 'projects']
    session: str = Field(pattern=r'^(rp-[0-9a-f]{32})?$')
    payload: dict


class SyncBatch(StrictModel):
    type: Literal['sync']
    stream: str = Field(pattern=r'^[0-9a-f]{32}$')
    entries: list[SyncEntry] = Field(max_length=32)


PAYLOADS = {'session': SessionData, 'event': EventData, 'terminal': TerminalData,
            'projects': ProjectsData, 'deleted': StrictModel}
