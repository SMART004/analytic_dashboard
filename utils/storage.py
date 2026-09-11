"""Compatibilité de stockage de fichiers via libSQL/Turso."""
from __future__ import annotations

import datetime
import hashlib
from io import BytesIO

import pandas as pd
import streamlit as st

from models.db import get_connection
from utils.helpers import load_file
from utils.turso_storage import dataframe_from_bytes, list_objects, load_bytes, save_bytes


def file_hash(file) -> str:
    content = file.getvalue() if hasattr(file, "getvalue") else file.read()
    if hasattr(file, "seek"):
        file.seek(0)
    return hashlib.md5(content).hexdigest()


def clean_filename(name: str) -> str:
    return name.replace(" ", "_").replace("/", "_")


def _path(bucket: str, name: str) -> str:
    return f"{bucket}/{name}"


def _objects(bucket: str, prefix: str = "") -> list[dict]:
    return list_objects(bucket, prefix)


def file_already_exists(file, bucket=None) -> bool:
    if not bucket:
        return False
    digest = file_hash(file)
    safe_name = clean_filename(file.name)
    return any(
        item["content_hash"] == digest and item["file_name"] == safe_name
        for item in _objects(bucket)
    )


def _save_uploaded(bucket: str, path: str, uploaded_file) -> None:
    save_bytes(
        bucket,
        path,
        uploaded_file.getvalue(),
        clean_filename(uploaded_file.name),
        getattr(uploaded_file, "type", None),
    )


def upload_with_folder(files, bucket):
    if not files:
        st.warning("Aucun fichier sélectionné")
        return None
    if not isinstance(files, list):
        files = [files]
    folder = datetime.datetime.now().strftime("%Y-%m-%d/%H-%M-%S")
    uploaded = 0
    for file in files:
        if not file_already_exists(file, bucket):
            _save_uploaded(bucket, f"{folder}/{clean_filename(file.name)}", file)
            uploaded += 1
    if uploaded:
        st.success(f"{uploaded} fichier(s) uploadé(s)")
    return folder


def upload_file(uploaded_file, bucket):
    if uploaded_file is None or file_already_exists(uploaded_file, bucket):
        return False
    _save_uploaded(bucket, f"{file_hash(uploaded_file)}__{clean_filename(uploaded_file.name)}", uploaded_file)
    return True


def _as_dataframe(bucket: str, path: str):
    content = load_bytes(bucket, path)
    if content is None:
        return None
    return dataframe_from_bytes(content, path)


@st.cache_data(show_spinner=False)
def get_with_folder(bucket, file_path):
    return _as_dataframe(bucket, file_path)


@st.cache_data(show_spinner=False, ttl=300)
def get_all_files(bucket: str) -> pd.DataFrame:
    frames = []
    for item in _objects(bucket):
        try:
            frame = _as_dataframe(bucket, item["name"])
            if frame is not None and not frame.empty:
                frame["source_file"] = item["file_name"]
                frame["source_path"] = item["name"]
                frames.append(frame)
        except Exception as exc:
            st.warning(f"Erreur lecture {item['name']}: {exc}")
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


@st.cache_data(show_spinner=False)
def get_one_folder(bucket):
    folders = list_month_folders(bucket)
    if not folders:
        return pd.DataFrame()
    frame = get_files_by_month(bucket, folders[-1])
    return frame if frame is not None else pd.DataFrame()


def extract_months_from_file(uploaded_file):
    try:
        frame = load_file(uploaded_file)
        if frame is None or "Date" not in frame.columns:
            return [datetime.date.today()]
        dates = pd.to_datetime(frame["Date"], errors="coerce").dropna()
        return [month.to_timestamp().date() for month in dates.dt.to_period("M").unique()]
    except Exception:
        return [datetime.date.today()]


def upload_file_by_month(uploaded_file, bucket):
    if uploaded_file is None or file_already_exists(uploaded_file, bucket):
        return False
    months = extract_months_from_file(uploaded_file)
    year = months[0].year
    folder = f"{year}-" + "-".join(f"{month.month:02d}" for month in sorted(months))
    _save_uploaded(bucket, f"{folder}/{file_hash(uploaded_file)}__{clean_filename(uploaded_file.name)}", uploaded_file)
    return True


@st.cache_data(ttl=120)
def list_month_folders(bucket=None):
    if not bucket:
        return []
    folders = {item["name"].split("/", 1)[0] for item in _objects(bucket) if "/" in item["name"]}
    return sorted(folders)


def get_month_files(bucket, selected_month):
    prefix = f"{selected_month}/"
    return [
        {"name": item["name"].split("/", 1)[1], "path": item["name"]}
        for item in _objects(bucket, prefix)
        if "/" in item["name"]
    ]


def download_month_file(bucket, selected_month, file_info):
    return _as_dataframe(bucket, file_info.get("path") or f"{selected_month}/{file_info['name']}")


@st.cache_data(ttl=60)
def get_files_by_month(bucket, selected_month, max_workers=1):
    frames = []
    for item in get_month_files(bucket, selected_month):
        frame = download_month_file(bucket, selected_month, item)
        if frame is not None and not frame.empty:
            frame["source_file"] = item["name"]
            frames.append(frame)
    return pd.concat(frames, ignore_index=True) if frames else None


def list_files(bucket):
    return [item["name"] for item in _objects(bucket)]


def get_file_bytes(bucket: str, file_path: str) -> bytes | None:
    return load_bytes(bucket, file_path)


get_files_bytes = get_file_bytes
