from importlib.metadata import version as distribution_version

project = "agroute"
release = distribution_version("agroute")
version = release
extensions = ["myst_parser"]
source_suffix = {".md": "markdown"}
master_doc = "index"
html_theme = "furo"
exclude_patterns = ["_build"]
