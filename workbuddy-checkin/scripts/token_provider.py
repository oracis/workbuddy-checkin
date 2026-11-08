#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 签到：登录态输入层（只读取用户显式提供的凭据）。

安全边界（本模块刻意不做的事）：
  * 不扫描客户端数据目录，不猜测凭据文件位置；
  * 不导入任何解密模块，不读取客户端进程内存；
  * 不写入任何凭据文件，不修改客户端登录态；
  * 不启动任何子进程。

accessToken 只可能来自下面两个「用户主动提供」的入口，按优先级取第一个：
  1. WB_ACCESS_TOKEN            环境变量（明文 JWT）
  2. WB_TOKEN_FILE             用户指定的 JSON 文件路径

JSON 形状（入口 2）：
  {"accessToken": "...", "refreshToken": "...", "uid": "...", "domain": "..."}
除 accessToken 外都可省略。拿不到就明确报错，绝不回退到系统里的其它文件。
"""
import json
import os
import re

_JWT_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
_FIELDS = ("accessToken", "refreshToken", "uid", "domain")


class CredentialError(Exception):
    """用户没有提供可用凭据，或提供的凭据形状不合法。"""


def _normalize(obj, source):
    """把外部 JSON 收敛成固定形状的 dict，丢弃一切多余字段。"""
    if not isinstance(obj, dict):
        raise CredentialError("%s 返回的不是 JSON 对象" % source)
    token = obj.get("accessToken")
    if not isinstance(token, str) or not _JWT_RE.match(token):
        raise CredentialError("%s 未提供合法的 accessToken（应为 JWT）" % source)
    out = {"token": token, "source": source}
    for key in _FIELDS[1:]:
        val = obj.get(key)
        if isinstance(val, str) and val:
            out["refresh" if key == "refreshToken" else key] = val
    return out


def from_env():
    """入口 1：环境变量。"""
    token = os.environ.get("WB_ACCESS_TOKEN")
    if not token:
        return None
    obj = {"accessToken": token}
    for env_key, field in (("WB_REFRESH_TOKEN", "refreshToken"),
                           ("WB_USER_ID", "uid"),
                           ("WB_DOMAIN", "domain")):
        if os.environ.get(env_key):
            obj[field] = os.environ[env_key]
    return _normalize(obj, "WB_ACCESS_TOKEN")


def from_file(path):
    """入口 2：用户显式指定的 JSON 文件。只打开这一个路径。

    拒绝符号链接 / Windows reparse point，避免读到被链接替换的目标；
    不做跨目录搜索，只按用户给的路径打开。
    """
    if not isinstance(path, str) or not path:
        return None
    if not os.path.isfile(path):
        raise CredentialError("WB_TOKEN_FILE 指向的文件不存在：%s" % path)
    real = os.path.realpath(path)
    if os.path.abspath(path) != real:
        raise CredentialError("WB_TOKEN_FILE 指向符号链接或重解析点，拒绝读取：%s" % path)
    try:
        fd = os.open(real, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            with os.fdopen(fd, "r", encoding="utf-8") as fh:
                obj = json.load(fh)
        except Exception as exc:
            raise CredentialError("WB_TOKEN_FILE 读取失败：%s" % exc)
    except CredentialError:
        raise
    except Exception as exc:
        raise CredentialError("WB_TOKEN_FILE 打开失败：%s" % exc)
    return _normalize(obj, "WB_TOKEN_FILE")


def resolve():
    """按优先级返回凭据 dict；都没有则抛出带指引的 CredentialError。"""
    for loader in (
        lambda: from_env(),
        lambda: from_file(os.environ.get("WB_TOKEN_FILE")),
    ):
        cred = loader()
        if cred:
            return cred

    raise CredentialError(
        "未提供登录凭据。本技能只使用用户显式提供的 accessToken，"
        "不会读取本机客户端的凭据文件。\n"
        "请任选其一：\n"
        "  1) 设置环境变量 WB_ACCESS_TOKEN（JWT）；\n"
        "  2) 设置 WB_TOKEN_FILE 指向含 accessToken 字段的 JSON 文件。\n"
        "若需要从本机 WorkBuddy 客户端会话取得 token，请另行安装本作者的 "
        "workbuddy-checkin-bridge 组件，由它解密后写出 JSON，再用入口 2 读取。"
    )
