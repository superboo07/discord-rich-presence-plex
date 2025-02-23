from config.constants import configFilePathBase
from utils.dict import copyDict
from utils.logging import logger
import json
import models.config
import os
import time
import yaml

config: models.config.Config = {
	"logging": {
		"debug": True,
		"writeToFile": False,
	},
	"display": {
		"hideTotalTime": False,
		"progressMode": "bar",
		"showDirector": True,
		"showEdition": True,
		"posters": {
			"enabled": False,
			"imgurClientID": "",
			"padPoster": True,
			"maxSize": 256
		},
		"buttons": [],
	},
	"users": [],
}
supportedConfigFileExtensions = {
	"yaml": "yaml",
	"yml": "yaml",
	"json": "json",
}
configFileExtension = ""
configFileType = ""
configFilePath = ""

def loadConfig() -> None:
	global configFileExtension, configFileType, configFilePath
	doesFileExist = False
	for i, (fileExtension, fileType) in enumerate(supportedConfigFileExtensions.items()):
		doesFileExist = os.path.isfile(f"{configFilePathBase}.{fileExtension}")
		isFirstItem = i == 0
		if doesFileExist or isFirstItem:
			configFileExtension = fileExtension
			configFileType = fileType
			configFilePath = f"{configFilePathBase}.{configFileExtension}"
			if doesFileExist:
				break
	if doesFileExist:
		try:
			with open(configFilePath, "r", encoding = "UTF-8") as configFile:
				if configFileType == "yaml":
					loadedConfig = yaml.safe_load(configFile) or {}
				else:
					loadedConfig = json.load(configFile) or {}
		except:
			os.rename(configFilePath, f"{configFilePathBase}-{time.time():.0f}.{configFileExtension}")
			logger.exception("Failed to parse the config file. A new one will be created.")
		else:
			copyDict(loadedConfig, config)
	saveConfig()

class YamlSafeDumper(yaml.SafeDumper):
    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        return super().increase_indent(flow, False)

def saveConfig() -> None:
	try:
		with open(configFilePath, "w", encoding = "UTF-8") as configFile:
			if configFileType == "yaml":
				yaml.dump(config, configFile, sort_keys = False, Dumper = YamlSafeDumper, allow_unicode = True)
			else:
				json.dump(config, configFile, indent = "\t")
				configFile.write("\n")
	except:
		logger.exception("Failed to write to the config file")
