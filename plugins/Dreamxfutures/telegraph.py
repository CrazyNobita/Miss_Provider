import os
import requests
from pyrogram import Client, filters
from pyrogram.types import Message


@Client.on_message(
    filters.command(["img", "cup", "telegraph"], prefixes="/") & filters.reply
)
async def c_upload(client, message: Message):
    reply = message.reply_to_message

    if not reply.media:
        return await message.reply_text("Reply to a media to upload it to Cloud.")

    if reply.document and reply.document.file_size > 10 * 1024 * 1024 * 1024:  # 10 GB
        return await message.reply_text("File size limit is 10 GB.")

    msg = await message.reply_text("Processing...")

    try:
        downloaded_media = await reply.download()

        if not downloaded_media:
            return await msg.edit_text("Something went wrong during download.")

        with open(downloaded_media, "rb") as f:
            data = f.read()
            resp = requests.post("https://graph.org", files={"file": data})
            if resp.status_code == 200:
                await msg.edit_text(f"`{resp.text}`")
            else:
                await msg.edit_text("Something went wrong. Please try again later.")

        os.remove(downloaded_media)

    except Exception as e:
        await msg.edit_text(f"Error: {str(e)}")
