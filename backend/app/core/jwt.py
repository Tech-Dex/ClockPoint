from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, Header
from jwt import PyJWTError, decode, encode
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic.networks import EmailStr
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.status import HTTP_403_FORBIDDEN, HTTP_404_NOT_FOUND

from app.core.config import settings
from app.core.database.mongodb import get_database
from app.models.enums.token_subject import TokenSubject
from app.models.token import TokenDB, TokenPayload
from app.models.user import UserDB, UserTokenWrapper
from app.repositories.token import save_token
from app.repositories.user import get_user_by_email


class TokenUtils:
    @classmethod
    async def wrap_user_db_data_into_token(
        cls,
        user_db: UserDB,
        subject: TokenSubject,
        group_id: str = None,
        user_email_invited: EmailStr = None,
        token_expires_delta: timedelta = timedelta(
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
        ),
    ) -> str:
        token: str = await cls.create_token(
            data={
                "email": user_db.email,
                "username": user_db.username,
                "group_id": group_id,
                "user_email_invited": user_email_invited,
            },
            expires_delta=token_expires_delta,
            subject=subject,
        )

        return token

    @staticmethod
    async def create_token(
        *, data: dict, expires_delta: timedelta = None, subject: TokenSubject
    ) -> str:
        to_encode: dict = data.copy()
        expire_datetime: datetime = datetime.utcnow() + timedelta(minutes=10)
        if expires_delta:
            expire_datetime: datetime = datetime.utcnow() + expires_delta
        to_encode.update({"exp": expire_datetime, "subject": subject.value})

        encoded_jwt = encode(
            to_encode, str(settings.SECRET_KEY), algorithm=settings.ALGORITHM
        )
        await save_token(
            TokenDB(
                **to_encode,
                token=encoded_jwt,
                created_at=datetime.utcnow(),
                expire_datetime=expire_datetime
            )
        )
        return encoded_jwt


def get_token(
    authorization: Optional[str] = Header(None),
    activation: Optional[str] = Header(None),
    recovery: Optional[str] = Header(None),
) -> str:
    token: str
    if authorization:
        prefix, token = authorization.split(" ")
        if settings.JWT_TOKEN_PREFIX != prefix:
            raise StarletteHTTPException(
                status_code=HTTP_403_FORBIDDEN, detail="Invalid authorization"
            )
        return token
    if activation:
        prefix, token = activation.split(" ")
        if settings.JWT_TOKEN_PREFIX != prefix:
            raise StarletteHTTPException(
                status_code=HTTP_403_FORBIDDEN, detail="Invalid activation"
            )
        return token
    if recovery:
        prefix, token = recovery.split(" ")
        if settings.JWT_TOKEN_PREFIX != prefix:
            raise StarletteHTTPException(
                status_code=HTTP_403_FORBIDDEN, detail="Invalid recover"
            )
        return token

    raise StarletteHTTPException(
        status_code=HTTP_403_FORBIDDEN, detail="Invalid header"
    )


async def get_current_user(
    conn: AsyncIOMotorClient = Depends(get_database),
    token: str = Depends(get_token),
) -> UserTokenWrapper:
    try:
        payload: dict = decode(
            token, str(settings.SECRET_KEY), algorithms=[settings.ALGORITHM]
        )
        token_data: TokenPayload = TokenPayload(**payload)
    except PyJWTError:
        raise StarletteHTTPException(
            status_code=HTTP_403_FORBIDDEN, detail="Could not validate credentials"
        )

    user_db: UserDB = await get_user_by_email(conn, token_data.email)
    if not user_db:
        raise StarletteHTTPException(
            status_code=HTTP_404_NOT_FOUND, detail="This user doesn't exist"
        )

    return UserTokenWrapper(**user_db.dict(), token=token)


async def get_invitation_token(invitation: str = Header(None)) -> str:
    prefix, token = invitation.split(" ")
    if settings.JWT_TOKEN_PREFIX != prefix:
        raise StarletteHTTPException(
            status_code=HTTP_403_FORBIDDEN, detail="Invalid invitation"
        )
    return token


async def get_user_from_invitation(
    conn: AsyncIOMotorClient = Depends(get_database),
    token: str = Depends(get_invitation_token),
) -> UserTokenWrapper:
    try:
        payload: dict = decode(
            token, str(settings.SECRET_KEY), algorithms=[settings.ALGORITHM]
        )
        if not payload.get("subject") in (
            TokenSubject.GROUP_INVITE_CO_OWNER,
            TokenSubject.GROUP_INVITE_MEMBER,
            TokenSubject.USER_INVITE,
        ):
            raise StarletteHTTPException(
                status_code=HTTP_403_FORBIDDEN, detail="This is not an invitation token"
            )
        token_data: TokenPayload = TokenPayload(**payload)
    except PyJWTError:
        raise StarletteHTTPException(
            status_code=HTTP_403_FORBIDDEN, detail="Could not validate invitation"
        )
    user_db: UserDB = await get_user_by_email(conn, token_data.user_email_invited)
    if not user_db:
        raise StarletteHTTPException(
            status_code=HTTP_404_NOT_FOUND, detail="This user doesn't exist"
        )

    return UserTokenWrapper(**user_db.dict(), token=token)


async def get_group_invitation(
    token: str = Depends(get_invitation_token),
) -> str:
    try:
        payload: dict = decode(
            token, str(settings.SECRET_KEY), algorithms=[settings.ALGORITHM]
        )
        if not payload.get("subject") in (
            TokenSubject.GROUP_INVITE_CO_OWNER,
            TokenSubject.GROUP_INVITE_MEMBER,
            TokenSubject.USER_INVITE,
        ):
            raise StarletteHTTPException(
                status_code=HTTP_403_FORBIDDEN, detail="This is not an invitation token"
            )
        return token
    except PyJWTError:
        raise StarletteHTTPException(
            status_code=HTTP_403_FORBIDDEN, detail="Could not validate invitation"
        )
