from __future__ import annotations

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator

from app.core.types import JsonObject


class OAuthTokenPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    access_token: StrictStr | None = None
    refresh_token: StrictStr | None = None
    id_token: StrictStr | None = None
    authorization_code: StrictStr | None = None
    code_verifier: StrictStr | None = None
    error: JsonObject | StrictStr | None = None
    error_description: StrictStr | None = None
    message: StrictStr | None = None
    error_code: StrictStr | None = None
    code: StrictStr | None = None
    status: StrictStr | None = None
    # Anthropic token-response extensions: identity arrives as organization /
    # account uuid fields, expiry as a relative ``expires_in`` (seconds).
    expires_in: int | None = None
    scope: StrictStr | None = None
    organization_uuid: StrictStr | None = Field(
        default=None,
        validation_alias=AliasChoices("organization_uuid", "organizationId"),
    )
    account_uuid: StrictStr | None = Field(
        default=None,
        validation_alias=AliasChoices("account_uuid", "accountId"),
    )
    email: StrictStr | None = None

    @model_validator(mode="before")
    @classmethod
    def _flatten_anthropic_identity(cls, data: object) -> object:
        """Accept Anthropic's nested identity shape.

        Sources disagree between flat ``organization_uuid``/``account_uuid``
        fields and nested ``organization: {uuid}`` / ``account: {uuid}``
        objects; flatten the nested form before validation."""
        if not isinstance(data, dict):
            return data
        flattened = dict(data)
        organization = flattened.pop("organization", None)
        if isinstance(organization, dict) and flattened.get("organization_uuid") is None:
            flattened["organization_uuid"] = organization.get("uuid")
        account = flattened.pop("account", None)
        if isinstance(account, dict):
            if flattened.get("account_uuid") is None:
                flattened["account_uuid"] = account.get("uuid")
            if flattened.get("email") is None:
                flattened["email"] = account.get("email")
        return flattened


class DeviceCodePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    device_auth_id: StrictStr | None = None
    user_code: StrictStr | None = Field(
        default=None,
        validation_alias=AliasChoices("user_code", "usercode"),
    )
    interval: int | None = None
    expires_in: int | None = None
    expires_at: StrictStr | None = None

    @field_validator("interval", mode="before")
    @classmethod
    def _parse_interval(cls, value: int | str | None) -> int | None:
        if value is None:
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            if stripped.isdigit():
                return int(stripped)
        raise ValueError("Invalid interval")
