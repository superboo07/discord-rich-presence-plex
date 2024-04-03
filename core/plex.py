# pyright: reportUnknownArgumentType=none,reportUnknownMemberType=none,reportUnknownVariableType=none

from .config import config
from .discord import DiscordIpcService
from .imgur import uploadToImgur
from config.constants import name, plexClientID
from plexapi.alert import AlertListener
from plexapi.base import PlexSession, PlexPartialObject
from plexapi.media import Genre, Guid
from plexapi.myplex import MyPlexAccount, PlexServer
from typing import Optional
from utils.cache import getCacheKey, setCacheKey
from utils.logging import LoggerWithPrefix
from utils.text import formatSeconds, truncate
import models.config
import models.discord
import models.plex
import requests
import threading
import time
import urllib.parse

def initiateAuth() -> tuple[str, str, str]:
	response = requests.post("https://plex.tv/api/v2/pins.json?strong=true", headers = {
		"X-Plex-Product": name,
		"X-Plex-Client-Identifier": plexClientID,
	}).json()
	authUrl = f"https://app.plex.tv/auth#?clientID={plexClientID}&code={response['code']}&context%%5Bdevice%%5D%%5Bproduct%%5D={urllib.parse.quote(name)}"
	return response["id"], response["code"], authUrl

def getAuthToken(id: str, code: str) -> Optional[str]:
	response = requests.get(f"https://plex.tv/api/v2/pins/{id}.json?code={code}", headers = {
		"X-Plex-Client-Identifier": plexClientID,
	}).json()
	return response["authToken"]

validMediaTypes = ["movie", "episode", "live_episode", "track", "clip"]
buttonTypeGuidTypeMap = {
	"imdb": "imdb",
	"tmdb": "tmdb",
	"thetvdb": "tvdb",
	"trakt": "tmdb",
	"letterboxd": "tmdb",
	"musicbrainz": "mbid",
}

class PlexAlertListener(threading.Thread):

	productName = "Plex Media Server"
	updateTimeoutTimerInterval = 30
	connectionCheckTimerInterval = 60
	maximumIgnores = 2

	def __init__(self, token: str, serverConfig: models.config.Server):
		super().__init__()
		self.daemon = True
		self.token = token
		self.serverConfig = serverConfig
		self.logger = LoggerWithPrefix(f"[{self.serverConfig['name']}] ") # pyright: ignore[reportTypedDictNotRequiredAccess]
		self.discordIpcService = DiscordIpcService(self.serverConfig.get("ipcPipeNumber"))
		self.updateTimeoutTimer: Optional[threading.Timer] = None
		self.connectionCheckTimer: Optional[threading.Timer] = None
		self.account: Optional[MyPlexAccount] = None
		self.server: Optional[PlexServer] = None
		self.alertListener: Optional[AlertListener] = None
		self.lastState, self.lastSessionKey, self.lastRatingKey = "", 0, 0
		self.listenForUser, self.isServerOwner, self.ignoreCount = "", False, 0
		self.start()

	def run(self) -> None:
		while True:
			try:
				self.logger.info("Signing into Plex")
				self.account = MyPlexAccount(token = self.token)
				self.logger.info("Signed in as Plex user '%s'", self.account.username)
				self.listenForUser = self.serverConfig.get("listenForUser", "") or self.account.username
				self.server = None
				for resource in self.account.resources():
					if resource.product == self.productName and resource.name.lower() == self.serverConfig["name"].lower():
						self.logger.info("Connecting to %s '%s'", self.productName, self.serverConfig["name"])
						self.server = resource.connect()
						try:
							self.server.account()
							self.isServerOwner = True
						except:
							pass
						self.logger.info("Connected to %s '%s'", self.productName, resource.name)
						self.alertListener = AlertListener(self.server, self.tryHandleAlert, self.reconnect)
						self.alertListener.start()
						self.logger.info("Listening for alerts from user '%s'", self.listenForUser)
						self.connectionCheckTimer = threading.Timer(self.connectionCheckTimerInterval, self.connectionCheck)
						self.connectionCheckTimer.start()
						return
				if not self.server:
					raise Exception("Server not found")
			except Exception as e:
				self.logger.error("Failed to connect to %s '%s': %s", self.productName, self.serverConfig["name"], e) # pyright: ignore[reportTypedDictNotRequiredAccess]
				self.logger.error("Reconnecting in 10 seconds")
				time.sleep(10)

	def disconnect(self) -> None:
		if self.alertListener:
			try:
				self.alertListener.stop()
			except:
				pass
		self.disconnectRpc()
		if self.connectionCheckTimer:
			self.connectionCheckTimer.cancel()
			self.connectionCheckTimer = None
		self.account, self.server, self.alertListener, self.listenForUser, self.isServerOwner, self.ignoreCount = None, None, None, "", False, 0
		self.logger.info("Stopped listening for alerts")

	def reconnect(self, exception: Exception) -> None:
		self.logger.error("Connection to Plex lost: %s", exception)
		self.disconnect()
		self.logger.error("Reconnecting")
		self.run()

	def disconnectRpc(self) -> None:
		self.lastState, self.lastSessionKey, self.lastRatingKey = "", 0, 0
		if self.discordIpcService.connected:
			self.discordIpcService.disconnect()
		if self.updateTimeoutTimer:
			self.updateTimeoutTimer.cancel()
			self.updateTimeoutTimer = None

	def updateTimeout(self) -> None:
		self.logger.debug("No recent updates from session key %s", self.lastSessionKey)
		self.disconnectRpc()

	def connectionCheck(self) -> None:
		try:
			self.logger.debug("Running periodic connection check")
			assert self.server
			self.server.clients()
		except Exception as e:
			self.reconnect(e)
		else:
			self.connectionCheckTimer = threading.Timer(self.connectionCheckTimerInterval, self.connectionCheck)
			self.connectionCheckTimer.start()

	def tryHandleAlert(self, alert: models.plex.Alert) -> None:
		try:
			self.handleAlert(alert)
		except:
			self.logger.exception("An unexpected error occured in the Plex alert handler")
			self.disconnectRpc()

	def handleAlert(self, alert: models.plex.Alert) -> None:
		if alert["type"] != "playing" or "PlaySessionStateNotification" not in alert:
			return
		stateNotification = alert["PlaySessionStateNotification"][0]
		self.logger.debug("Received alert: %s", stateNotification)
		ratingKey = int(stateNotification["ratingKey"])
		assert self.server
		item: PlexPartialObject = self.server.fetchItem(ratingKey)
		if item.key and item.key.startswith("/livetv"):
			mediaType = "live_episode"
		else:
			mediaType: str = item.type
		if mediaType not in validMediaTypes:
			self.logger.debug("Unsupported media type '%s', ignoring", mediaType)
			return
		state = stateNotification["state"]
		sessionKey = int(stateNotification["sessionKey"])
		viewOffset = int(stateNotification["viewOffset"])
		try:
			libraryName: str = item.section().title
		except:
			libraryName = "ERROR"
		if "blacklistedLibraries" in self.serverConfig and libraryName in self.serverConfig["blacklistedLibraries"]:
			self.logger.debug("Library '%s' is blacklisted, ignoring", libraryName)
			return
		if "whitelistedLibraries" in self.serverConfig and libraryName not in self.serverConfig["whitelistedLibraries"]:
			self.logger.debug("Library '%s' is not whitelisted, ignoring", libraryName)
			return
		if self.lastSessionKey == sessionKey and self.lastRatingKey == ratingKey:
			if self.updateTimeoutTimer:
				self.updateTimeoutTimer.cancel()
				self.updateTimeoutTimer = None
			if self.lastState == state and self.ignoreCount < self.maximumIgnores:
				self.logger.debug("Nothing changed, ignoring")
				self.ignoreCount += 1
				self.updateTimeoutTimer = threading.Timer(self.updateTimeoutTimerInterval, self.updateTimeout)
				self.updateTimeoutTimer.start()
				return
			else:
				self.ignoreCount = 0
				if state == "stopped":
					self.disconnectRpc()
					return
		elif state == "stopped":
			self.logger.debug("Received 'stopped' state alert from unknown session, ignoring")
			return
		if self.isServerOwner:
			self.logger.debug("Searching sessions for session key %s", sessionKey)
			sessions: list[PlexSession] = self.server.sessions()
			if len(sessions) < 1:
				self.logger.debug("Empty session list, ignoring")
				return
			for session in sessions:
				self.logger.debug("%s, Session Key: %s, Usernames: %s", session, session.sessionKey, session.usernames)
				if session.sessionKey == sessionKey:
					self.logger.debug("Session found")
					sessionUsername: str = session.usernames[0]
					if sessionUsername.lower() == self.listenForUser.lower():
						self.logger.debug("Username '%s' matches '%s', continuing", sessionUsername, self.listenForUser)
						break
					self.logger.debug("Username '%s' doesn't match '%s', ignoring", sessionUsername, self.listenForUser)
					return
			else:
				self.logger.debug("No matching session found, ignoring")
				return
		if self.updateTimeoutTimer:
			self.updateTimeoutTimer.cancel()
		self.updateTimeoutTimer = threading.Timer(self.updateTimeoutTimerInterval, self.updateTimeout)
		self.updateTimeoutTimer.start()
		self.lastState, self.lastSessionKey, self.lastRatingKey = state, sessionKey, ratingKey
		stateStrings: list[str] = []
		if mediaType == "movie":
			title = shortTitle = item.title
			if item.editionTitle:
				stateStrings.append(item.editionTitle)
			largeText = item.title
			thumb = item.thumb
		elif mediaType == "episode":
			title = shortTitle = item.title
			grandparent = self.server.fetchItem(item.grandparentRatingKey)
			stateStrings.append(f"S{item.parentIndex:02}E{item.index:02}")
			largeText = item.grandparentTitle
			thumb = item.grandparentThumb
		elif mediaType == "live_episode":
			title = shortTitle = item.grandparentTitle
			if item.title != item.grandparentTitle:
				stateStrings.append(item.title)
			largeText = "Watching live TV"
			thumb = item.grandparentThumb
		elif mediaType == "track":
			title = shortTitle = item.title
			artistAlbum = f"{item.originalTitle or item.grandparentTitle}"
			stateStrings.append(artistAlbum)
			largeText = f"{item.parentTitle}"
			thumb = item.thumb
		else:
			title = shortTitle = item.title
			largeText = "Watching a video"
			thumb = item.thumb
		if state != "playing" and mediaType != "track":
			if config["display"]["useRemainingTime"]:
				stateStrings.append(f"{formatSeconds((item.duration - viewOffset) / 1000, ':')} left")
			else:
				stateStrings.append(f"{formatSeconds(viewOffset / 1000, ':')} elapsed")
		stateText = " · ".join(stateString for stateString in stateStrings if stateString)
		thumbUrl = ""
		if thumb and config["display"]["posters"]["enabled"]:
			thumbUrl = getCacheKey(thumb)
			if not thumbUrl or not isinstance(thumbUrl, str):
				self.logger.debug("Uploading poster to Imgur")
				thumbUrl = uploadToImgur(self.server.url(thumb, True), config["display"]["posters"]["maxSize"], config["display"]["posters"]["padPoster"])
				setCacheKey(thumb, thumbUrl)
		activity: models.discord.Activity = {
			"details": truncate(title, 128),
			"assets": {
				"large_text": largeText,
				"large_image": thumbUrl or "logo",
				"small_text": state.capitalize(),
				"small_image": state,
			},
		}
		if stateText:
			activity["state"] = truncate(stateText, 128)
		if config["display"]["buttons"]:
			guidsRaw: list[Guid] = []
			if mediaType in ["movie", "track"]:
				guidsRaw = item.guids
			elif mediaType == "episode":
				guidsRaw = self.server.fetchItem(item.grandparentRatingKey).guids
			guids: dict[str, str] = { guidSplit[0]: guidSplit[1] for guidSplit in [guid.id.split("://") for guid in guidsRaw] if len(guidSplit) > 1 }
			buttons: list[models.discord.ActivityButton] = []
			for button in config["display"]["buttons"]:
				if "mediaTypes" in button and mediaType not in button["mediaTypes"]:
					continue
				label = truncate(button["label"].format(title = shortTitle), 32)
				if not button["url"].startswith("dynamic:"):
					buttons.append({ "label": label, "url": button["url"] })
					continue
				buttonType = button["url"][8:]
				guidType = buttonTypeGuidTypeMap.get(buttonType)
				if not guidType:
					continue
				guid = guids.get(guidType)
				if not guid:
					continue
				url = ""
				if buttonType == "imdb":
					url = f"https://www.imdb.com/title/{guid}"
				elif buttonType == "tmdb":
					tmdbPathSegment = "movie" if mediaType == "movie" else "tv"
					url = f"https://www.themoviedb.org/{tmdbPathSegment}/{guid}"
				elif buttonType == "thetvdb":
					theTvdbPathSegment = "movie" if mediaType == "movie" else "series"
					url = f"https://www.thetvdb.com/dereferrer/{theTvdbPathSegment}/{guid}"
				elif buttonType == "trakt":
					idType = "movie" if mediaType == "movie" else "show"
					url = f"https://trakt.tv/search/tmdb/{guid}?id_type={idType}"
				elif buttonType == "letterboxd" and mediaType == "movie":
					url = f"https://letterboxd.com/tmdb/{guid}"
				elif buttonType == "musicbrainz":
					url = f"https://musicbrainz.org/track/{guid}"
				if url:
					buttons.append({ "label": label, "url": url })
			if buttons:
				activity["buttons"] = buttons[:2]
		if state == "playing":
			currentTimestamp = int(time.time())
			if config["display"]["useRemainingTime"]:
				activity["timestamps"] = {"end": round(currentTimestamp + ((item.duration - viewOffset) / 1000))}
			else:
				activity["timestamps"] = {"start": round(currentTimestamp - (viewOffset / 1000))}
		if not self.discordIpcService.connected:
			self.discordIpcService.connect()
		if self.discordIpcService.connected:
			self.discordIpcService.setActivity(activity)
