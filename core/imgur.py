from .config import config
from PIL import Image
from PIL import ImageOps
from typing import Optional
from utils.logging import logger
import io
import models.imgur
import requests

def uploadToImgur(url: str, maxSize: int = 0, padPoster: bool = False) -> Optional[str]:
	try:
		originalImageBytesIO = io.BytesIO(requests.get(url).content)
		originalImage = Image.open(originalImageBytesIO)
		newImage = Image.new("RGBA", originalImage.size)
		newImage.putdata(originalImage.getdata()) # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
		if maxSize:
			newImage.thumbnail((maxSize, maxSize))
		newImageBytesIO = io.BytesIO()
		if padPoster:
			newImage = ImageOps.pad(newImage, (maxSize, maxSize), color=(255,255,255,0))
		newImage.save(newImageBytesIO, subsampling = 0, quality = 90, format = "PNG")
		data: models.imgur.UploadResponse = requests.post(
			"https://api.imgur.com/3/image",
			headers = { "Authorization": f"Client-ID {config['display']['posters']['imgurClientID']}" },
			files = { "image": newImageBytesIO.getvalue() }
		).json()
		if not data["success"]:
			raise Exception(data["data"]["error"])
		return data["data"]["link"]
	except:
		logger.exception("An unexpected error occured while uploading an image to Imgur")
