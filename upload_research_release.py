#!/usr/bin/env python3
"""upload_research_release.py - 把 research.zip 上传到 GitHub Release。"""
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error

OWNER = "monologue-github"
REPO = "stock-analyzer"
TAG = "v4.0.2-research"
NAME = "v4.0.2 research data"
BODY = "全A策略消融 + v4.0.2 研究报告数据压缩包（含 strategy_ablation_per_stock.json / strategy_ablation_summary.json / v4_report.json / v4_factors.json / legacy 等）。"
ASSET_PATH = "research.zip"


def _get_token():
    """从 git credential manager 读取 GitHub token。"""
    inp = "protocol=https\nhost=github.com\n\n"
    try:
        out = subprocess.run(
            ["git", "credential", "fill"],
            input=inp, text=True, capture_output=True, check=True
        ).stdout
    except Exception as e:
        print(f"无法从 git credential 读取 token: {e}")
        return None
    for line in out.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1]
    return None


def _api_request(url, method="GET", data=None, headers=None):
    req = urllib.request.Request(url, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    if data is not None:
        if isinstance(data, dict):
            payload = json.dumps(data).encode("utf-8")
            req.add_header("Content-Type", "application/json")
        else:
            payload = data
        req.data = payload
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def main():
    token = _get_token()
    if not token:
        print("未获取到 GitHub token，退出")
        sys.exit(1)

    auth_headers = {"Authorization": f"Bearer {token}"}

    # 1. 检查 release 是否已存在
    releases_url = f"https://api.github.com/repos/{OWNER}/{REPO}/releases/tags/{TAG}"
    status, body = _api_request(releases_url, headers=auth_headers)
    if status == 200:
        rel = json.loads(body)
        release_id = rel["id"]
        upload_url = rel["upload_url"].replace("{?name,label}", "")
        print(f"Release {TAG} 已存在，id={release_id}")
    else:
        # 2. 创建 release
        create_url = f"https://api.github.com/repos/{OWNER}/{REPO}/releases"
        status, body = _api_request(
            create_url, method="POST",
            data={"tag_name": TAG, "name": NAME, "body": BODY},
            headers=auth_headers
        )
        if status not in (200, 201):
            print(f"创建 release 失败: {status}\n{body}")
            sys.exit(1)
        rel = json.loads(body)
        release_id = rel["id"]
        upload_url = rel["upload_url"].replace("{?name,label}", "")
        print(f"Release {TAG} 创建成功，id={release_id}")

    # 3. 检查是否已有同名 asset，有则删除
    status, body = _api_request(f"https://api.github.com/repos/{OWNER}/{REPO}/releases/{release_id}", headers=auth_headers)
    if status == 200:
        rel = json.loads(body)
        for asset in rel.get("assets", []):
            if asset["name"] == os.path.basename(ASSET_PATH):
                print(f"删除旧 asset {asset['name']} (id={asset['id']})")
                _api_request(
                    f"https://api.github.com/repos/{OWNER}/{REPO}/releases/assets/{asset['id']}",
                    method="DELETE", headers=auth_headers
                )

    # 4. 上传 asset
    upload_url_with_name = f"{upload_url}?name={os.path.basename(ASSET_PATH)}"
    print(f"上传 {ASSET_PATH} ({os.path.getsize(ASSET_PATH)/1024/1024:.1f} MB) ...")
    with open(ASSET_PATH, "rb") as f:
        data = f.read()
    status, body = _api_request(
        upload_url_with_name, method="POST", data=data,
        headers={**auth_headers, "Content-Type": "application/zip"}
    )
    if status not in (200, 201):
        print(f"上传 asset 失败: {status}\n{body}")
        sys.exit(1)
    asset = json.loads(body)
    print(f"上传成功: {asset['browser_download_url']}")


if __name__ == "__main__":
    main()
