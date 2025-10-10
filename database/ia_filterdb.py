import logging
from struct import pack
import re
import base64
from pyrogram.file_id import FileId
from typing import Dict, List
from collections import defaultdict
from pymongo.errors import DuplicateKeyError
from umongo import Instance, Document, fields
from motor.motor_asyncio import AsyncIOMotorClient
from marshmallow import ValidationError
from info import *
from utils import get_settings, save_group_settings
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
# ---------------------------------------------------------

# Global cache for DB size
_db_stats_cache = {"timestamp": None, "primary_size": 0.0}

# Primary DB
client = AsyncIOMotorClient(DATABASE_URI)
db = client[DATABASE_NAME]
instance = Instance.from_db(db)

# secondary db
client2 = AsyncIOMotorClient(DATABASE_URI2)
db2 = client2[DATABASE_NAME]
instance2 = Instance.from_db(db2)


@instance.register
class Media(Document):
    file_id = fields.StrField(attribute="_id")
    file_name = fields.StrField(required=True)
    file_size = fields.IntField(required=True)

    class Meta:
        indexes = ("$file_name",)
        collection_name = COLLECTION_NAME


@instance2.register
class Media2(Document):
    file_id = fields.StrField(attribute="_id")
    file_name = fields.StrField(required=True)
    file_size = fields.IntField(required=True)
    
    class Meta:
        indexes = ("$file_name",)
        collection_name = COLLECTION_NAME


async def check_db_size(db):
    try:
        now = datetime.utcnow()
        cache_stale_by_time = _db_stats_cache["timestamp"] is None or (
            now - _db_stats_cache["timestamp"] > timedelta(minutes=10)
        )
        refresh_if_size_threshold = _db_stats_cache["primary_size"] >= 10.0
        if not cache_stale_by_time and not refresh_if_size_threshold:
            return _db_stats_cache["primary_size"]
        stats = await db.command("dbstats")
        db_logical_size = stats["dataSize"]
        db_index_size = stats["indexSize"]
        db_logical_size_mb = db_logical_size / (1024 * 1024)
        db_index_size_mb = db_index_size / (1024 * 1024)
        db_size_mb = db_logical_size_mb + db_index_size_mb
        _db_stats_cache["primary_size"] = db_size_mb
        _db_stats_cache["timestamp"] = now
        return db_size_mb
    except Exception as e:
        print(f"Error Checking Database Size: {e}")
        return 0


async def save_file(media):
    """Save file in database, with detailed logging."""
    file_id, file_ref = unpack_new_file_id(media.file_id)
    file_name = re.sub(
        r"[_\-\.#+$%^&*()!~`,;:\"'?/<>\[\]{}=|\\]", " ", str(media.file_name)
    )
    file_name = re.sub(r"\s+", " ", file_name).strip()
    saveMedia = Media
    target_db = "Primary"
    if MULTIPLE_DB:
        try:
            exists = await Media.count_documents({"file_id": file_id}, limit=1)
            if exists:
                logger.info(f"[SKIP] '{file_name}' already in Primary DB.")
                return False, 0
            primary_db_size = await check_db_size(db)
            if primary_db_size >= 407:
                saveMedia = Media2
                target_db = "Secondary"
                logger.warning("Switching to Secondary DB due to size threshold.")
        except Exception as e:
            logger.error(
                "Error during MULTIPLE_DB check; defaulting to primary DB.", exc_info=e
            )
    try:
        record = saveMedia(
            file_id=file_id,
            file_name=file_name,
            file_size=media.file_size,
        )
    except ValidationError as e:
        logger.exception(f"[VALIDATION ERROR] '{file_name}' → {e}")
        return False, 2
    try:
        await record.commit()
    except DuplicateKeyError:
        logger.info(
            f"[SKIP] DuplicateKey: '{file_name}' already exists in {target_db} DB."
        )
        return False, 0
    except Exception as e:
        logger.exception(
            f"[ERROR] Failed commit of '{file_name}' to {target_db} DB.", exc_info=e
        )
        return False, 3
    logger.info(f"[SUCCESS] '{file_name}' saved to {target_db} DB.")
    return True, 1


async def get_search_results(
    chat_id, query, file_type=None, max_results=10, offset=0, filter=False
):
    if chat_id is not None:
        settings = await get_settings(int(chat_id))
        try:
            max_results = 10 if settings.get("max_btn") else int(MAX_B_TN)
        except KeyError:
            await save_group_settings(int(chat_id), "max_btn", False)
            settings = await get_settings(int(chat_id))
            max_results = 10 if settings.get("max_btn") else int(MAX_B_TN)
    if isinstance(query, list):
        regex_list = []
        for q in query:
            q = q.strip()
            if not q:
                continue
            if " " not in q:
                raw = r"(\b|[\.\+\-_])" + re.escape(q) + r"(\b|[\.\+\-_])"
            else:
                raw = re.escape(q).replace(r"\ ", r".*[\s\.\+\-_()]")
            regex_list.append(re.compile(raw, re.IGNORECASE))

        if USE_CAPTION_FILTER:
            filter_mongo = {
                "$or": (
                    [{"file_name": r} for r in regex_list]
                    + [{"caption": r} for r in regex_list]
                )
            }
        else:
            filter_mongo = {"$or": [{"file_name": r} for r in regex_list]}

    else:
        query = query.strip()
        if not query:
            raw_pattern = "."
        elif " " not in query:
            raw_pattern = r"(\b|[\.\+\-_])" + query + r"(\b|[\.\+\-_])"
        else:
            raw_pattern = query.replace(
                " ", r".*[\s\.\+\-_()\[\]]" 
            )

        try:
            regex = re.compile(raw_pattern, flags=re.IGNORECASE)
        except re.error:
            return [], "", 0

        if USE_CAPTION_FILTER:
            filter_mongo = {"$or": [{"file_name": regex}, {"caption": regex}]}
        else:
            filter_mongo = {"file_name": regex}
    if file_type:
        filter_mongo["file_type"] = file_type
    total_results = await Media.count_documents(filter_mongo)
    if MULTIPLE_DB:
        total_results += await Media2.count_documents(filter_mongo)

    # if max_results % 2:
    #     max_results += 1

    cursor1 = (
        Media.find(filter_mongo).sort("$natural", -1).skip(offset).limit(max_results)
    )
    files1 = await cursor1.to_list(length=max_results)

    if MULTIPLE_DB:
        remaining = max_results - len(files1)
        cursor2 = (
            Media2.find(filter_mongo).sort("$natural", -1).skip(offset).limit(remaining)
        )
        files2 = await cursor2.to_list(length=remaining)
        files = files1 + files2
    else:
        files = files1
    next_offset = offset + len(files)
    if next_offset >= total_results:
        next_offset = ""
    return files, next_offset, total_results


async def get_bad_files(query, file_type=None):
    query = query.strip()
    if not query:
        raw_pattern = '.'
    elif ' ' not in query:
        raw_pattern = r"(\b|[\.\+\-_])" + query + r"(\b|[\.\+\-_])"
    else:
        raw_pattern = query.replace(" ", r".*[\s\.\+\-_()]")
    try:
        regex = re.compile(raw_pattern, flags=re.IGNORECASE)
    except:
        return []
    if USE_CAPTION_FILTER:
        filter = {'$or': [{'file_name': regex}, {'caption': regex}]}
    else:
        filter = {'file_name': regex}
    if file_type:
        filter['file_type'] = file_type
    cursor1 = Media.find(filter).sort('$natural', -1)
    files1 = await cursor1.to_list(length=(await Media.count_documents(filter)))
    if MULTIPLE_DB:
        cursor2 = Media2.find(filter).sort('$natural', -1)
        files2 = await cursor2.to_list(length=(await Media2.count_documents(filter)))
        files = files1 + files2
    else:
        files = files1
    total_results = len(files)
    return files, total_results


async def get_file_details(query):
    filter = {"file_id": query}
    cursor = Media.find(filter)
    filedetails = await cursor.to_list(length=1)
    if not filedetails:
        cursor2 = Media2.find(filter)
        filedetails = await cursor2.to_list(length=1)
    return filedetails


def encode_file_id(s: bytes) -> str:
    r = b""
    n = 0
    for i in s + bytes([22]) + bytes([4]):
        if i == 0:
            n += 1
        else:
            if n:
                r += b"\x00" + bytes([n])
                n = 0

            r += bytes([i])
    return base64.urlsafe_b64encode(r).decode().rstrip("=")


def encode_file_ref(file_ref: bytes) -> str:
    return base64.urlsafe_b64encode(file_ref).decode().rstrip("=")


def unpack_new_file_id(new_file_id):
    """Return file_id, file_ref"""
    decoded = FileId.decode(new_file_id)
    file_id = encode_file_id(
        pack(
            "<iiqq",
            int(decoded.file_type),
            decoded.dc_id,
            decoded.media_id,
            decoded.access_hash,
        )
    )
    file_ref = encode_file_ref(decoded.file_reference)
    return file_id, file_ref


async def dreamxbotz_fetch_media(limit: int) -> List[dict]:
    try:
        if MULTIPLE_DB:
            db_size = await check_db_size(Media)
            if db_size > 407:
                cursor = Media2.find().sort("$natural", -1).limit(limit)
                files = await cursor.to_list(length=limit)
                return files
        cursor = Media.find().sort("$natural", -1).limit(limit)
        files = await cursor.to_list(length=limit)
        return files
    except Exception as e:
        logger.error(f"Error in dreamxbotz_fetch_media: {e}")
        return []


async def dreamxbotz_clean_title(filename: str, is_series: bool = False) -> str:
    try:
        year_match = re.search(r"^(.*?(\d{4}|\(\d{4}\)))", filename, re.IGNORECASE)
        if year_match:
            title = year_match.group(1).replace("(", "").replace(")", "")
            return (
                re.sub(
                    r"(?:@[^ \n\r\t.,:;!?()\[\]{}<>\\\/\"'=_%]+|[._\-\[\]@()]+)",
                    " ",
                    title,
                )
                .strip()
                .title()
            )
        if is_series:
            season_match = re.search(
                r"(.*?)(?:S(\d{1,2})|Season\s*(\d+)|Season(\d+))(?:\s*Combined)?",
                filename,
                re.IGNORECASE,
            )
            if season_match:
                title = season_match.group(1).strip()
                season = (
                    season_match.group(2)
                    or season_match.group(3)
                    or season_match.group(4)
                )
                title = (
                    re.sub(
                        r"(?:@[^ \n\r\t.,:;!?()\[\]{}<>\\\/\"'=_%]+|[._\-\[\]@()]+)",
                        " ",
                        title,
                    )
                    .strip()
                    .title()
                )
                return f"{title} S{int(season):02}"
        title = filename
        return (
            re.sub(
                r"(?:@[^ \n\r\t.,:;!?()\[\]{}<>\\\/\"'=_%]+|[._\-\[\]@()]+)", " ", title
            )
            .strip()
            .title()
        )
    except Exception as e:
        logger.error(f"Error in truncate_title: {e}")
        return filename


async def dreamxbotz_get_movies(limit: int = 20) -> List[str]:
    try:
        cursor = await dreamxbotz_fetch_media(limit * 2)
        results = set()
        pattern = r"(?:s\d{1,2}|season\s*\d+|season\d+)(?:\s*combined)?(?:e\d{1,2}|episode\s*\d+)?\b"
        for file in cursor:
            file_name = getattr(file, "file_name", "")
            if not re.search(pattern, file_name, re.IGNORECASE):
                title = await dreamxbotz_clean_title(file_name)
                results.add(title)
            if len(results) >= limit:
                break
        return sorted(list(results))[:limit]
    except Exception as e:
        logger.error(f"Error in dreamxbotz_get_movies: {e}")
        return []


async def dreamxbotz_get_series(limit: int = 30) -> Dict[str, List[int]]:
    try:
        cursor = await dreamxbotz_fetch_media(limit * 5)
        grouped = defaultdict(list)
        pattern = r"(.*?)(?:S(\d{1,2})|Season\s*(\d+)|Season(\d+))(?:\s*Combined)?(?:E(\d{1,2})|Episode\s*(\d+))?\b"
        for file in cursor:
            file_name = getattr(file, "file_name", "")
            match = re.search(pattern, file_name, re.IGNORECASE)
            if match:
                title = await dreamxbotz_clean_title(match.group(1), is_series=True)
                season = int(match.group(2) or match.group(3) or match.group(4))
                grouped[title].append(season)
        return {
            title: sorted(set(seasons))[:10]
            for title, seasons in grouped.items()
            if seasons
        }
    except Exception as e:
        logger.error(f"Error in dreamxbotz_get_series: {e}")
        return []
