import os
import requests
from pyrogram import Client, filters
from pyrogram.types import Message

@Client.on_message(filters.command(["img", "cup", "telegraph"], prefixes="/") & filters.reply)
async def c_upload(client, message: Message):
    reply = message.reply_to_message
    if not reply or not reply.media:
        return await message.reply_text("📸 Reply to an image, video, or file to upload it to Telegraph.")
    
    msg = await message.reply_text("📤 Uploading to Telegraph...")

    try:
        # Download file first
        downloaded = await reply.download()
        if not downloaded:
            return await msg.edit_text("❌ Failed to download media.")

        # Upload to Telegraph
        with open(downloaded, "rb") as f:
            response = requests.post(
                "https://telegra.ph/upload",
                files={"file": f}
            )

        os.remove(downloaded)

        if response.status_code == 200:
            result = response.json()
            if isinstance(result, list) and "src" in result[0]:
                telegraph_url = "https://telegra.ph" + result[0]["src"]
                await msg.edit_text(f"✅ Uploaded Successfully!\n\n🔗 {telegraph_url}")
            else:
                await msg.edit_text("⚠️ Upload failed! Telegraph returned invalid response.")
        else:
            await msg.edit_text("❌ Telegraph upload error. Please try again later.")

    except Exception as e:
        await msg.edit_text(f"⚠️ Error: {str(e)}")
