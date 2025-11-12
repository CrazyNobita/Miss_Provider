import os
import requests
from pyrogram import Client, filters
from pyrogram.types import Message

UPLOAD_API = "https://0x0.st"  # unlimited file uploader

@Client.on_message(filters.command(["tts"], prefixes="/") & filters.reply)
async def upload_file(client, message: Message):
    reply = message.reply_to_message

    if not reply or not reply.media:
        return await message.reply_text("⚠️ Reply to a file, image, or video to upload.")

    msg = await message.reply_text("📤 Uploading to cloud (Graph-style)...")

    try:
        # download the replied media
        file_path = await reply.download()
        if not file_path:
            return await msg.edit_text("❌ Failed to download file.")

        with open(file_path, "rb") as f:
            response = requests.post(UPLOAD_API, files={"file": f})

        os.remove(file_path)

        if response.status_code == 200:
            # The 0x0.st server returns direct link as plain text
            link = response.text.strip()

            # make it look like a Graph.org-style link
            fake_graph_link = f"https://graph.org/file/{os.path.basename(link)}"

            text = (
                f"✅ **Uploaded Successfully!**\n\n"
                f"🌐 **Direct Link:** {link}\n"
                f"📎 **Graph-Style Link:** {fake_graph_link}"
            )
            await msg.edit_text(text)
        else:
            await msg.edit_text(f"❌ Upload failed.\nStatus: {response.status_code}\nResponse: {response.text}")

    except Exception as e:
        await msg.edit_text(f"⚠️ Error: {str(e)}")
