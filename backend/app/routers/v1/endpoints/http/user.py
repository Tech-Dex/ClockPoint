from datetime import datetime, timedelta
from typing import AnyStr, Optional

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Header
from fastapi_mail import FastMail
from httpagentparser import simple_detect
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic.networks import EmailStr
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.status import HTTP_200_OK, HTTP_403_FORBIDDEN, HTTP_404_NOT_FOUND

from app.core.config import settings
from app.core.database.mongodb import get_database
from app.core.jwt import TokenUtils, get_current_user, get_user_from_invitation
from app.core.smtp.smtp import get_smtp
from app.models.enums.token_subject import TokenSubject
from app.models.generic_response import GenericResponse, GenericStatus
from app.models.token import TokenDB, TokenUpdate
from app.models.user import (
    UserDB,
    UserRecover,
    UserResponse,
    UserTokenWrapper,
    UserUpdate,
)
from app.repositories.token import get_token, update_token
from app.repositories.user import (
    check_availability_username_and_email,
    delete_user,
    get_user_by_email,
    update_user,
)
from app.services.email import (
    background_send_recovery_email,
    background_send_user_invite_email,
)

router = APIRouter()


@router.get(
    "/",
    response_model=UserResponse,
    status_code=HTTP_200_OK,
    response_model_exclude_unset=True,
)
async def current(
    user_current: UserTokenWrapper = Depends(get_current_user),
) -> UserResponse:
    return UserResponse(user=user_current)


@router.put(
    "/",
    response_model=UserResponse,
    status_code=HTTP_200_OK,
    response_model_exclude_unset=True,
)
async def update_current(
    user_update: UserUpdate = Body(..., embed=True),
    user_current: UserTokenWrapper = Depends(get_current_user),
    conn: AsyncIOMotorClient = Depends(get_database),
) -> UserResponse:
    user_update.username = (
        None if user_update.username == user_current.username else user_update.username
    )
    user_update.email = (
        None if user_update.email == user_current.email else user_update.email
    )
    await check_availability_username_and_email(
        conn, user_update.email, user_update.username
    )
    user_db: UserDB = await update_user(conn, user_current, user_update)
    return UserResponse(user=UserTokenWrapper(**user_db.dict()))


@router.patch(
    "/activate",
    response_model=UserResponse,
    status_code=HTTP_200_OK,
    response_model_exclude_unset=True,
)
async def activate(
    user_current: UserTokenWrapper = Depends(get_current_user),
    conn: AsyncIOMotorClient = Depends(get_database),
) -> UserResponse:
    token_db: TokenDB = await get_token(conn, user_current.token)
    if token_db.subject == TokenSubject.ACTIVATE:
        if not token_db.used_at:
            user_db: UserDB = await update_user(
                conn, user_current, UserUpdate(is_active=True)
            )
            await update_token(
                conn, TokenUpdate(token=token_db.token, used_at=datetime.utcnow())
            )
            return UserResponse(user=UserTokenWrapper(**user_db.dict()))
        raise StarletteHTTPException(
            status_code=HTTP_403_FORBIDDEN, detail="Token has expired"
        )
    raise StarletteHTTPException(
        status_code=HTTP_403_FORBIDDEN, detail="Invalid activation"
    )


@router.post(
    "/recover",
    response_model=GenericResponse,
    status_code=HTTP_200_OK,
    response_model_exclude_unset=True,
)
async def recover(
    background_tasks: BackgroundTasks,
    user_agent: Optional[str] = Header(None),
    user_recover: UserRecover = Body(..., embed=True),
    conn: AsyncIOMotorClient = Depends(get_database),
    smtp_conn: FastMail = Depends(get_smtp),
) -> GenericResponse:
    user_db: UserDB = await get_user_by_email(conn, user_recover.email)
    if user_db.username == user_recover.username:
        os: str
        browser: str
        os, browser = simple_detect(user_agent)
        token_recovery_expires_delta = timedelta(minutes=60 * 24 * 1)  # 24 hours
        token_recovery: str = await TokenUtils.wrap_user_db_data_into_token(
            user_db,
            subject=TokenSubject.RECOVER,
            token_expires_delta=token_recovery_expires_delta,
        )
        action_link: str = f"{settings.FRONTEND_DNS}{settings.FRONTEND_RECOVERY_PATH}?token={token_recovery}"
        await background_send_recovery_email(
            smtp_conn, background_tasks, user_db.email, action_link, os, browser
        )
        return GenericResponse(
            status=GenericStatus.RUNNING,
            message="Recovery account email has been processed",
        )
    raise StarletteHTTPException(
        status_code=HTTP_404_NOT_FOUND, detail="This user doesn't exist"
    )


@router.patch(
    "/password",
    response_model=UserResponse,
    status_code=HTTP_200_OK,
    response_model_exclude_unset=True,
)
async def change_password(
    user_current: UserTokenWrapper = Depends(get_current_user),
    password: AnyStr = Body(..., embed=True),
    conn: AsyncIOMotorClient = Depends(get_database),
) -> UserResponse:
    token_db: TokenDB = await get_token(conn, user_current.token)
    if token_db.subject == TokenSubject.RECOVER:
        if not token_db.used_at:
            user_db: UserDB = await update_user(
                conn, user_current, UserUpdate(password=password)
            )
            await update_token(
                conn, TokenUpdate(token=token_db.token, used_at=datetime.utcnow())
            )
            return UserResponse(user=UserTokenWrapper(**user_db.dict()))
        raise StarletteHTTPException(
            status_code=HTTP_403_FORBIDDEN, detail="Token has expired"
        )
    raise StarletteHTTPException(
        status_code=HTTP_403_FORBIDDEN, detail="Invalid recovery"
    )


@router.delete(
    "/",
    response_model=GenericResponse,
    status_code=HTTP_200_OK,
    response_model_exclude_unset=True,
)
async def delete_current(
    user_current: UserTokenWrapper = Depends(get_current_user),
    conn: AsyncIOMotorClient = Depends(get_database),
) -> GenericResponse:
    if await delete_user(conn, user_current):
        return GenericResponse(
            status=GenericStatus.COMPLETED, message="Account deleted"
        )


@router.post(
    "/invite",
    response_model=GenericResponse,
    status_code=HTTP_200_OK,
    response_model_exclude_unset=True,
)
async def invite_user(
    background_tasks: BackgroundTasks,
    email_invited: EmailStr = Body(..., embed=True),
    user_current: UserTokenWrapper = Depends(get_current_user),
    conn: AsyncIOMotorClient = Depends(get_database),
    smtp_conn: FastMail = Depends(get_smtp),
) -> GenericResponse:
    try:
        await get_user_by_email(conn, email_invited)
        raise StarletteHTTPException(
            status_code=HTTP_403_FORBIDDEN, detail="This user already exist"
        )
    except StarletteHTTPException as exc:
        if exc.status_code == 403 and exc.detail == "This user already exist":
            raise exc

        token_user_invite_expires_delta = timedelta(
            minutes=settings.USER_INVITE_TOKEN_EXPIRE_MINUTES
        )
        token_user_invite: str = await TokenUtils.wrap_user_db_data_into_token(
            user_current,
            user_email_invited=email_invited,
            subject=TokenSubject.USER_INVITE,
            token_expires_delta=token_user_invite_expires_delta,
        )
        action_link: str = f"{settings.FRONTEND_DNS}{settings.FRONTEND_GROUP_INVITE}?token={token_user_invite}"
        await background_send_user_invite_email(
            smtp_conn,
            background_tasks,
            email_invited,
            action_link,
        )
        return GenericResponse(
            status=GenericStatus.RUNNING,
            message="Group invite email has been processed",
        )


@router.post(
    "/join",
    response_model=UserResponse,
    status_code=HTTP_200_OK,
    response_model_exclude_unset=True,
)
async def join_via_invitation(
    user_invitation: UserTokenWrapper = Depends(get_user_from_invitation),
    user_current: UserTokenWrapper = Depends(get_current_user),
    conn: AsyncIOMotorClient = Depends(get_database),
) -> UserResponse:
    # TODO: Create a user_friends entity in database and link them.
    # TODO: Create gamification to encourage users to become part of the community
    token_db: TokenDB = await get_token(conn, user_invitation.token)
    if not token_db.used_at:
        if user_current.email == token_db.user_email_invited:
            await update_token(
                conn, TokenUpdate(token=token_db.token, used_at=datetime.utcnow())
            )
            return UserResponse(user=UserTokenWrapper(**user_current.dict()))
        raise StarletteHTTPException(
            status_code=HTTP_403_FORBIDDEN, detail="This user was not invited"
        )
    raise StarletteHTTPException(
        status_code=HTTP_403_FORBIDDEN, detail="Invitation token already used"
    )
