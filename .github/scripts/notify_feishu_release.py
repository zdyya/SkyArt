#!/usr/bin/env python3
"""发版通知：把 DMG 传到飞书云空间，再往群机器人 webhook 发一张卡片。

用法（供 GitHub Actions 调用，凭证走环境变量，不接受命令行传密钥）：
    python3 notify_feishu_release.py \
        --title "SkyArt v1.0.3" \
        --changelog-file changelog.txt \
        --dmg-path electron-app/dist/SkyArt-1.0.3-arm64.dmg \
        --release-url https://github.com/xxx/releases/tag/v1.0.3

环境变量：
    FEISHU_APP_ID / FEISHU_APP_SECRET   自建应用凭证（云空间上传用）
    FEISHU_FOLDER_TOKEN                 目标云空间文件夹 token
    FEISHU_WEBHOOK_URL                  群自定义机器人 webhook
    FEISHU_WEBHOOK_KEYWORD              webhook 安全设置里配的自定义关键词（默认 SkyArt）
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

API_BASE = "https://open.feishu.cn/open-apis"
BLOCK_SIZE = 4 * 1024 * 1024  # 4MB，Feishu 分片上传单片大小


def call_json(method, path, token=None, body=None):
    url = API_BASE + path
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path} 失败: HTTP {e.code} {e.read().decode('utf-8')}")


def call_multipart(path, token, fields, file_field, file_bytes, file_name):
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n")
    body = "".join(parts).encode("utf-8")
    body += (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
        f"filename=\"{file_name}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8")
    body += file_bytes
    body += f"\r\n--{boundary}--\r\n".encode("utf-8")

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }
    req = urllib.request.Request(API_BASE + path, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"POST {path} 失败: HTTP {e.code} {e.read().decode('utf-8')}")


def get_tenant_access_token(app_id, app_secret):
    resp = call_json("POST", "/auth/v3/tenant_access_token/internal", body={
        "app_id": app_id,
        "app_secret": app_secret,
    })
    if resp.get("code") != 0:
        raise RuntimeError(f"获取 tenant_access_token 失败: {resp}")
    return resp["tenant_access_token"]


def upload_file(token, folder_token, file_path):
    file_size = os.path.getsize(file_path)
    file_name = os.path.basename(file_path)

    if file_size <= BLOCK_SIZE:
        with open(file_path, "rb") as f:
            data = f.read()
        resp = call_multipart(
            "/drive/v1/files/upload_all",
            token,
            {
                "file_name": file_name,
                "parent_type": "explorer",
                "parent_node": folder_token,
                "size": str(file_size),
            },
            "file",
            data,
            file_name,
        )
        if resp.get("code") != 0:
            raise RuntimeError(f"上传文件失败: {resp}")
        return resp["data"]["file_token"]

    prepare_resp = call_json(
        "POST",
        "/drive/v1/files/upload_prepare",
        token=token,
        body={
            "file_name": file_name,
            "parent_type": "explorer",
            "parent_node": folder_token,
            "size": file_size,
        },
    )
    if prepare_resp.get("code") != 0:
        raise RuntimeError(f"分片上传初始化失败: {prepare_resp}")
    upload_id = prepare_resp["data"]["upload_id"]
    block_num = prepare_resp["data"]["block_num"]

    with open(file_path, "rb") as f:
        for seq in range(block_num):
            chunk = f.read(BLOCK_SIZE)
            resp = call_multipart(
                "/drive/v1/files/upload_part",
                token,
                {"upload_id": upload_id, "seq": str(seq), "size": str(len(chunk))},
                "file",
                chunk,
                file_name,
            )
            if resp.get("code") != 0:
                raise RuntimeError(f"上传分片 {seq} 失败: {resp}")
            print(f"  分片 {seq + 1}/{block_num} 已上传")

    finish_resp = call_json(
        "POST",
        "/drive/v1/files/upload_finish",
        token=token,
        body={"upload_id": upload_id, "block_num": block_num},
    )
    if finish_resp.get("code") != 0:
        raise RuntimeError(f"分片上传完成失败: {finish_resp}")
    return finish_resp["data"]["file_token"]


def get_file_url(token, file_token):
    resp = call_json(
        "POST",
        "/drive/v1/metas/batch_query",
        token=token,
        body={
            "request_docs": [{"doc_token": file_token, "doc_type": "file"}],
            "with_url": True,
        },
    )
    metas = resp.get("data", {}).get("metas", [])
    if metas and metas[0].get("url"):
        return metas[0]["url"]
    return None


def arch_label(file_name):
    """DMG 文件名 → 卡片上给运营看的机型说明。"""
    if "arm64" in file_name:
        return "Apple 芯片（M 系列）"
    if "x64" in file_name:
        return "Intel 芯片"
    return file_name


def send_card(webhook_url, keyword, title, changelog, downloads, release_url):
    lines = [f"**变更点**\n{changelog}", ""]
    for label, url in downloads:
        lines.append(f"📦 [{label} · 点击下载]({url})")
    if release_url:
        lines.append(f"\n🔧 [GitHub Release（工程内部用）]({release_url})")
    content_md = "\n".join(lines)

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"[{keyword}] {title}"},
                "template": "blue",
            },
            "elements": [{"tag": "markdown", "content": content_md}],
        },
    }
    data = json.dumps(card).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"发送 webhook 失败: HTTP {e.code} {e.read().decode('utf-8')}")
    if result.get("code") not in (0, None):
        raise RuntimeError(f"webhook 返回错误: {result}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", required=True)
    parser.add_argument("--changelog-file", required=True)
    # 一次发版有多个架构的包，每个 --dmg-path 传一次
    parser.add_argument("--dmg-path", action="append", required=True)
    parser.add_argument("--release-url", default=None)
    args = parser.parse_args()

    app_id = os.environ["FEISHU_APP_ID"]
    app_secret = os.environ["FEISHU_APP_SECRET"]
    folder_token = os.environ["FEISHU_FOLDER_TOKEN"]
    webhook_url = os.environ["FEISHU_WEBHOOK_URL"]
    keyword = os.environ.get("FEISHU_WEBHOOK_KEYWORD", "SkyArt")

    with open(args.changelog_file, "r", encoding="utf-8") as f:
        changelog = f.read().strip()

    token = get_tenant_access_token(app_id, app_secret)
    print("已获取 tenant_access_token")

    downloads = []
    for dmg_path in args.dmg_path:
        file_name = os.path.basename(dmg_path)
        file_token = upload_file(token, folder_token, dmg_path)
        print(f"{file_name} 已上传到云空间: file_token={file_token}")

        drive_url = get_file_url(token, file_token)
        if not drive_url:
            print(f"获取 {file_name} 的云空间分享链接失败", file=sys.stderr)
            sys.exit(1)
        print(f"云空间链接: {drive_url}")
        downloads.append((arch_label(file_name), drive_url))

    # Apple 芯片排前面：现在绝大多数机器是 M 系列
    downloads.sort(key=lambda d: 0 if "Apple" in d[0] else 1)

    send_card(webhook_url, keyword, args.title, changelog, downloads, args.release_url)
    print("飞书通知已发送")


if __name__ == "__main__":
    main()
