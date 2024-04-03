from config.constants import isInContainer, runtimeDirectory, uid, gid, containerCwd, noRuntimeDirChown
from utils.logging import logger
import webbrowser
import os

if isInContainer:
	if not os.path.isdir(runtimeDirectory):
		logger.error(f"Runtime directory does not exist. Ensure that it is mounted into the container at {runtimeDirectory}")
		exit(1)
	if os.geteuid() == 0: # pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType]
		if uid == -1 or gid == -1:
			logger.warning(f"Environment variable(s) DRPP_UID and/or DRPP_GID are/is not set. Manually ensure appropriate ownership of {runtimeDirectory}")
			statResult = os.stat(runtimeDirectory)
			uid, gid = statResult.st_uid, statResult.st_gid
		else:
			if noRuntimeDirChown:
				logger.warning(f"DRPP_NO_RUNTIME_DIR_CHOWN is set to true. Manually ensure appropriate ownership of {runtimeDirectory}")
			else:
				os.system(f"chmod 700 {runtimeDirectory}")
				os.system(f"chown -R {uid}:{gid} {runtimeDirectory}")
		os.system(f"chown -R {uid}:{gid} {containerCwd}")
		os.setgid(gid) # pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType]
		os.setuid(uid) # pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType]
	else:
		logger.warning(f"Not running as the superuser. Manually ensure appropriate ownership of mounted contents")

from config.constants import noPipInstall
import sys

if not noPipInstall:
	try:
		import subprocess
		def parsePipPackages(packagesStr: str) -> dict[str, str]:
			return { packageSplit[0].lower(): packageSplit[1] if len(packageSplit) > 1 else "" for packageSplit in [package.split("==") for package in packagesStr.splitlines()] }
		pipFreezeResult = subprocess.run([sys.executable, "-m", "pip", "freeze"], stdout = subprocess.PIPE, text = True, check = True)
		installedPackages = parsePipPackages(pipFreezeResult.stdout)
		with open("requirements.txt", "r", encoding = "UTF-8") as requirementsFile:
			requiredPackages = parsePipPackages(requirementsFile.read())
		for packageName, requiredPackageVersion in requiredPackages.items():
			installedPackageVersion = installedPackages.get(packageName, "none")
			if installedPackageVersion != requiredPackageVersion:
				logger.info(f"Installing dependency: {packageName} (required: {requiredPackageVersion}, installed: {installedPackageVersion})")
				subprocess.run([sys.executable, "-m", "pip", "install", "-U", f"{packageName}=={requiredPackageVersion}"], check = True)
	except Exception as e:
		logger.exception("An unexpected error occured during automatic installation of dependencies. Install them manually by running the following command: python -m pip install -U -r requirements.txt")

from config.constants import dataDirectoryPath, logFilePath, name, version, isInteractive
from core.config import config, loadConfig, saveConfig
from core.discord import DiscordIpcService
from core.plex import PlexAlertListener, initiateAuth, getAuthToken
from typing import Optional
from utils.cache import loadCache
from utils.logging import formatter
from utils.text import formatSeconds
import logging
import models.config
import time

def init() -> None:
	if not os.path.isdir(dataDirectoryPath):
		os.makedirs(dataDirectoryPath)
	for oldFilePath in ["config.json", "cache.json", "console.log"]:
		if os.path.isfile(oldFilePath):
			os.rename(oldFilePath, os.path.join(dataDirectoryPath, oldFilePath))
	loadConfig()
	if config["logging"]["debug"]:
		logger.setLevel(logging.DEBUG)
	if config["logging"]["writeToFile"]:
		fileHandler = logging.FileHandler(logFilePath)
		fileHandler.setFormatter(formatter)
		logger.addHandler(fileHandler)
	logger.info("%s - v%s", name, version)
	loadCache()

def main() -> None:
	init()
	if not config["users"]:
		logger.info("No users found in the config file")
		user = authNewUser()
		if not user:
			exit(1)
		config["users"].append(user)
		saveConfig()
	plexAlertListeners = [PlexAlertListener(user["token"], server) for user in config["users"] for server in user["servers"]]
	try:
		if isInteractive:
			while True:
				userInput = input()
				if userInput in ["exit", "quit"]:
					raise KeyboardInterrupt
		else:
			while True:
				time.sleep(3600)
	except KeyboardInterrupt:
		for plexAlertListener in plexAlertListeners:
			plexAlertListener.disconnect()

def authNewUser() -> Optional[models.config.User]:
	id, code, url = initiateAuth()
	logger.info("Please sign in using the browser window that has opened, or use the below URL:")
	logger.info(url)
	webbrowser.open(url)
	time.sleep(5)
	for i in range(35):
		logger.info(f"Checking whether authentication is successful ({formatSeconds((i + 1) * 5)})")
		authToken = getAuthToken(id, code)
		if authToken:
			logger.info("Authentication successful")
			serverName = os.environ.get("DRPP_PLEX_SERVER_NAME_INPUT")
			if not serverName:
				serverName = input("Enter the name of the Plex Media Server you wish to connect to: ") if isInteractive else "ServerName"
			return { "token": authToken, "servers": [{ "name": serverName }] }
		time.sleep(5)
	else:
		logger.info(f"Authentication timed out ({formatSeconds(180)})")

def testIpc(pipeNumber: int) -> None:
	init()
	logger.info("Testing Discord IPC connection")
	discordIpcService = DiscordIpcService(pipeNumber)
	discordIpcService.connect()
	discordIpcService.setActivity({
		"details": "details",
		"state": "state",
		"assets": {
			"large_text": "large_text",
			"large_image": "logo",
			"small_text": "small_text",
			"small_image": "playing",
		},
	})
	time.sleep(15)
	discordIpcService.disconnect()

if __name__ == "__main__":
	mode = sys.argv[1] if len(sys.argv) > 1 else ""
	try:
		if not mode:
			main()
		elif mode == "test-ipc":
			testIpc(int(sys.argv[2]) if len(sys.argv) > 2 else -1)
		else:
			logger.error(f"Invalid mode: {mode}")
	except KeyboardInterrupt:
		pass
