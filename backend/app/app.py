import asyncio
from functools import reduce
import hashlib
from http import HTTPStatus
import json
from operator import contains
import os
from threading import Thread
import time
from urllib import request
import uuid
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.datastructures import FormData
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel
from datetime import datetime, timedelta
from jose import JWTError, jwt
from passlib.context import CryptContext
import requests
from app.models.users import *
import app.util as util
import app.db.localDb
from app.websocketManager import WSManager


SECRET_KEY = "83daa0256a2289b0fb23693bf1f6034d44396675749244721a2b20e896e11662"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

fastApp = FastAPI()
db = app.db.localDb.LocalDb()
wsMan = WSManager()
msgQueue = []


def create_access_token(data: dict, expires_delta: timedelta | None = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)

    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


async def get_current_user(token: str = Depends(oauth2_scheme)):
    credential_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str | None = payload.get("sub")
        if username is None:
            raise credential_exception

        token_data = TokenData(username=username)
    except JWTError:
        raise credential_exception

    user = db.get_user(username)

    return user if user else credential_exception
    # if user is None:
    #     raise credential_exception

    # return user


async def get_current_active_user(current_user: UserInDB = Depends(get_current_user)):
    # if current_user.disabled:
    #     raise HTTPException(status_code=400, detail="Inactive user")

    return current_user


@fastApp.post("/register")
async def register_user(registerUser: UserInDB):
    isAlready = not db.addRegisterUser(registerUser)
    print(f"REG {registerUser}")
    if isAlready:
        raise HTTPException(status_code=400, detail="User ALready Exist")
    return "Lol"


@fastApp.post("/token", response_model=Token)
async def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends()):
    user = db.authenticate_user(form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.username}, expires_delta=access_token_expires
    )

    return {"access_token": access_token, "token_type": "bearer"}


@fastApp.get("/users/me/", response_model=UserBase)
async def read_users_me(current_user: UserBase = Depends(get_current_active_user)):
    return current_user


@fastApp.get("/users/me/items")
async def read_own_items(current_user: UserBase = Depends(get_current_active_user)):
    return [{"item_id": 1, "owner": current_user}]


@fastApp.get("/user/{username}/publickey")
async def get_user_public_key(
    username, user: UserInDB = Depends(get_current_active_user)
):
    print(username)
    recUser = db.get_user(username)
    if not recUser:
        raise HTTPException(status_code=400, detail="Invalid Receiver username")
    return {"publicKey": recUser.publicKeyStr, "username": recUser.username}


@fastApp.post("/attachment/")
async def upload(
    file: UploadFile = File(...), user: UserInDB = Depends(get_current_active_user)
):
    try:
        if file.filename == None:
            return HTTPException(status_code=status.HTTP_406_NOT_ACCEPTABLE)

        contents = file.file.read()
        imgId = hashlib.md5(contents).hexdigest()

        for i in os.listdir("uploadImages"):
            if imgId == i.split(".")[0]:
                return {"imgId": imgId}

        with open(f"./uploadImages/{imgId}.{file.filename.split('.')[1]}", "wb") as f:
            f.write(contents)

    except Exception:
        return HTTPException(status_code=status.HTTP_406_NOT_ACCEPTABLE)
    finally:
        file.file.close()
    return {"imgId": imgId}


@fastApp.get("/attachment/{imgId}")
async def get_attachment(imgId, user: UserInDB = Depends(get_current_active_user)):
    if imgId is None or imgId == "":
        return

    for i in os.listdir("uploadImages"):
        if imgId == i.split(".")[0]:
            return FileResponse(path=f"./uploadImages/{i}")


@fastApp.websocket("/ws")
async def chat_user(socket: WebSocket, token: str = Query(...)):
    try:
        current_user: UserInDB = await get_current_user(token)  # type: ignore
        # if not isinstance(current_user, UserInDB):
        #     print(f"Disconnect {current_user}")
        #     await socket.close()
        #     return
    except HTTPException as e:
        print("Token error")
        await socket.close()
        return e

    await wsMan.connect(current_user.username, socket)
    await socket.send_text(f"PING from Server to user {current_user.username}")
    await check_msg_queue()
    while True:
        try:
            data = await socket.receive_text()
            pd = json.loads(data)
            pd["msg"] = hashlib.sha256(data.encode()).hexdigest()
            print(f"Current User {current_user.username} {pd}")
            recData = json.loads(data)
            if (
                ("to" not in recData)
                or ("msg" not in recData)
                or ("timestamp" not in recData)
            ):
                print("Invalid DATA ", recData)
                continue

            recData.update({"from": current_user.username})
            recSock = wsMan.getUserSocket(recData["to"])
            recDb = db.get_user(recData["to"])
            if not recSock:
                if not recDb:
                    print("User not found")
                else:
                    print("Msg Queue Add")
                    msgQueue.append(recData)
                continue

            # print(json.dumps(recData))
            await recSock.send_text(json.dumps(recData))

        except Exception as e:
            print(e)
            if type(e) != json.decoder.JSONDecodeError:
                # print(socket.conn)
                print(socket in wsMan.connections)
                wsMan.removeSocket(socket)
                break
            pass
    print(
        f"{socket in wsMan.connections} {list(filter(lambda z: socket == z[1] ,wsMan.userCon.items()))}"
    )


async def check_msg_queue():
    sendedIndx = []
    for i in range(len(msgQueue)):
        recSock = wsMan.getUserSocket(msgQueue[i]["to"])
        if recSock:
            print("te")
            await recSock.send_text(json.dumps(msgQueue[i]))
            sendedIndx.append(i)
    try:
        for i in sendedIndx:
            # error here
            msgQueue.pop(i)
    except Exception as e:
        pass


@fastApp.get("/{username}")
async def getUser(username):
    print(username)
    if username not in ("aab", "bbb"):
        raise HTTPException(status_code=404)

    user = db.authenticate_user(username, "aab" if username == "aab" else "bb")
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.username}, expires_delta=access_token_expires
    )

    return {"access_token": access_token, "token_type": "bearer"}
