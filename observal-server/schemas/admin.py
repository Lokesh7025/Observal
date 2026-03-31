import uuid

from pydantic import BaseModel


class EnterpriseConfigResponse(BaseModel):
    key: str
    value: str
    model_config = {"from_attributes": True}


class EnterpriseConfigUpdate(BaseModel):
    value: str


class UserAdminResponse(BaseModel):
    id: uuid.UUID
    email: str
    name: str
    role: str
    model_config = {"from_attributes": True}


class UserRoleUpdate(BaseModel):
    role: str
