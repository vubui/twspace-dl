"""Script designed to help download twitter spaces"""
import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import cached_property
from urllib.parse import urlparse

import requests


# mostly taken from ytarchive
class FormatInfo(dict):
    """
    Simple class to more easily keep track of what fields are available for
    file name formatting
    """

    DEFAULT_FNAME_FORMAT = "[%(creator_name)s]%(title)s-%(id)s"

    def __init__(self):
        dict.__init__(
            self,
            {
                "id": "",
                "url": "",
                "title": "",
                "creator_name": "",
                "creator_screen_name": "",
                "start_date": "",
            },
        )

    def set_info(self, metadata: dict) -> None:
        root = defaultdict(str, metadata["data"]["audioSpace"]["metadata"])
        self["id"] = root["rest_id"]
        self["url"] = "https://twitter.com/spaces/" + self["id"]
        self["title"] = root["title"]
        self["creator_name"] = root["creator_results"]["result"]["legacy"]["name"]
        self["creator_screen_name"] = root["creator_results"]["result"]["legacy"][
            "screen_name"
        ]
        self["start_date"] = datetime.fromtimestamp(
            int(root["started_at"]) / 1000
        ).strftime("%Y-%m-%d")

    @staticmethod
    def sterilize_fn(filename: str) -> str:
        bad_chars = '<>:"/\\|?*'
        for char in bad_chars:
            filename.replace(char, "_")
        return filename

    def format(self, format_str: str) -> str:
        return format_str % self


class TwspaceDL:
    """Downloader class for twitter spaces"""

    def __init__(self, url: str, threads: int, format_str: str):
        if not url:
            logging.warning("No space url given, file won't have any metadata")
            self.id = "no_id"
            self.format_str = "no_info"
        else:
            space_id = re.findall(r"(?<=spaces/)\w*", url)[0]
            self.id = space_id
            self.format_str = format_str or FormatInfo.DEFAULT_FNAME_FORMAT
        self.threads = threads
        self.progress = 0
        self.total_segments: int

    @cached_property
    def _guest_token(self) -> str:
        response = requests.get("https://twitter.com/").text
        last_line = response.splitlines()[-1]
        guest_token = re.findall(r"(?<=gt\=)\d{19}", last_line)[0]
        logging.debug(guest_token)
        return guest_token

    @property
    def metadata(self) -> dict:
        """Get space metadata"""
        params = {
            "variables": (
                "{"
                f'"id":"{self.id}",'
                '"isMetatagsQuery":false,'
                '"withSuperFollowsUserFields":true,'
                '"withUserResults":true,'
                '"withBirdwatchPivots":false,'
                '"withReactionsMetadata":false,'
                '"withReactionsPerspective":false,'
                '"withSuperFollowsTweetFields":true,'
                '"withReplays":true,'
                '"withScheduledSpaces":true'
                "}"
            )
        }
        headers = {
            "authorization": (
                "Bearer "
                "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
                "=1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
            ),
            "x-guest-token": self._guest_token,
        }
        response = requests.get(
            "https://twitter.com/i/api/graphql/jyQ0_DEMZHeoluCgHJ-U5Q/AudioSpaceById",
            params=params,
            headers=headers,
        )
        metadata = response.json()
        try:
            media_key = metadata["data"]["audioSpace"]["metadata"]["media_key"]
            logging.debug(media_key)
        except KeyError as error:
            logging.error(metadata)
            raise RuntimeError(metadata) from error
        return metadata

    @cached_property
    def filename(self):
        format_info = FormatInfo()
        format_info.set_info(self.metadata)
        filename = format_info.format(self.format_str)
        return filename

    def write_metadata(self) -> None:
        """Write the metadata to a file"""
        metadata = json.dumps(self.metadata, indent=4)
        filename = self.filename
        with open(f"{filename}.json", "w", encoding="utf-8") as metadata_io:
            metadata_io.write(metadata)
            logging.info(f"{filename}.json written to disk")

    @cached_property
    def dyn_url(self) -> str:
        metadata = self.metadata
        if metadata["data"]["audioSpace"]["metadata"]["state"] == "Ended":
            logging.error(
                (
                    "Can't Download. Space has ended, can't retrieve master url. "
                    "You can provide it with -f URL if you have it."
                )
            )
            raise ValueError("Space Ended")
        headers = {
            "authorization": (
                "Bearer "
                "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
                "=1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
            ),
            "cookie": "auth_token=",
        }
        media_key = metadata["data"]["audioSpace"]["metadata"]["media_key"]
        response = requests.get(
            "https://twitter.com/i/api/1.1/live_video_stream/status/" + media_key,
            headers=headers,
        )
        try:
            metadata = response.json()
        except Exception as err:
            raise RuntimeError("Space isn't available") from err
        dyn_url = metadata["source"]["location"]
        return dyn_url

    @cached_property
    def master_url(self) -> str:
        """Master URL for a space"""
        master_url = self.dyn_url.removesuffix("?type=live").replace(
            "dynamic", "master"
        )
        return master_url

    @property
    def playlist_url(self) -> str:
        """Get the URL containing the chunks filenames"""
        response = requests.get(self.master_url)
        playlist_suffix = response.text.splitlines()[3]
        domain = urlparse(self.master_url).netloc
        playlist_url = f"https://{domain}{playlist_suffix}"
        return playlist_url

    @property
    def playlist_text(self) -> str:
        """Modify the chunks URL using the master one to be able to download"""
        playlist_text = requests.get(self.playlist_url).text
        master_url_wo_file = self.master_url.removesuffix("master_playlist.m3u8")
        playlist_text = re.sub(r"(?=chunk)", master_url_wo_file, playlist_text)
        return playlist_text

    def write_playlist(self, save_dir: str = "./") -> None:
        """Write the modified playlist for external use"""
        filename = self.filename
        with open(
            os.path.join(save_dir, f"{filename}.m3u8"), "w", encoding="utf-8"
        ) as stream_io:
            stream_io.write(self.playlist_text)
        logging.info(f"{filename}.m3u8 written to disk")

    def download(self) -> None:
        """Download a twitter space"""
        if not shutil.which("ffmpeg"):
            raise FileNotFoundError("ffmpeg not installed")
        metadata = self.metadata
        os.makedirs("tmp", exist_ok=True)
        self.write_playlist(save_dir="tmp")
        format_info = FormatInfo()
        format_info.set_info(metadata)
        state = metadata["data"]["audioSpace"]["metadata"]["state"]

        cmd_base = [
            "ffmpeg",
            "-y",
            "-stats",
            "-v",
            "warning",
            "-i",
            "-c",
            "copy",
            "-metadata",
            f"title='{format_info['title']}'",
            "-metadata",
            f"author='{format_info['creator_name']}'",
            "-metadata",
            f"episode_id='{self.id}'",
            os.path.join("tmp", f"{self.filename}.m4a"),
        ]
        cmd_old = (
            [cmd_base[0]]
            + [
                "-protocol_whitelist",
                "file,https,tls,tcp",
            ]
            + cmd_base[1:6]
            + [
                os.path.join("tmp", self.filename + ".m3u8"),
            ]
            + cmd_base[6:]
        )

        if state == "Running":
            cmd_new = (
                cmd_base[:6]
                + [self.dyn_url]
                + cmd_base[6:-1]
                + [os.path.join("tmp", f"{self.filename}_new.m4a")]
            )

            cmd_final = (
                cmd_base[:6]
                + [
                    (
                        "concat:"
                        + os.path.join("tmp", f"{self.filename}.m4a")
                        + "|"
                        + os.path.join("tmp", f"{self.filename}_new.m4a")
                    )
                ]
                + cmd_base[6:-1]
                + [f"{self.filename}.m4a"]
            )

            with ThreadPoolExecutor(max_workers=self.threads) as executor:
                executor.map(subprocess.run, (cmd_new, cmd_old), timeout=60)
            subprocess.run(cmd_final, check=True)
        else:
            subprocess.run(cmd_old, check=True)
            shutil.move(
                os.path.join("tmp", self.filename + ".m4a"), self.filename + ".m4a"
            )

        logging.info("Finished downloading")


def get_args():
    parser = argparse.ArgumentParser(
        description="Script designed to help download twitter spaces"
    )
    parser.add_argument("-i", "--input-url", type=str, metavar="SPACE_URL")
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        metavar="FORMAT_STR",
    )
    parser.add_argument(
        "-f",
        "--from-master-url",
        type=str,
        metavar="URL",
        help="use the master url for the processes(useful for ended spaces)",
    )
    parser.add_argument(
        "-t",
        "--threads",
        type=int,
        metavar="THREADS",
        help="number of threads to run the script with(default with max)",
        default=os.cpu_count(),
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "-m",
        "--write-metadata",
        action="store_true",
        help="write the full metadata json to a file",
    )
    parser.add_argument(
        "-p",
        "--write-playlist",
        action="store_true",
        help=(
            "write the m3u8 used to download the stream"
            "(e.g. if you want to use another downloader)"
        ),
    )
    parser.add_argument(
        "-u", "--url", action="store_true", help="display the master url"
    )
    parser.add_argument("-s", "--skip-download", action="store_true")
    parser.add_argument("-k", "--keep-files", action="store_true")
    if len(sys.argv) == 1:
        parser.print_help(sys.stderr)
        sys.exit(1)
    args = parser.parse_args()
    return args


if __name__ == "__main__":
    args = get_args()
    if not args.input_url and not args.from_master_url:
        print("Either space url or master url should be provided")
        sys.exit(1)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    twspace_dl = TwspaceDL(args.input_url, args.threads, args.output)
    if args.from_master_url:
        twspace_dl.master_url = args.from_master_url
    if args.write_metadata:
        twspace_dl.write_metadata()
    if args.url:
        print(twspace_dl.master_url)
    if args.write_playlist:
        twspace_dl.write_playlist()
    if not args.skip_download:
        try:
            twspace_dl.download()
        except KeyboardInterrupt:
            logging.info("Download Interrupted")
        finally:
            if not args.keep_files and os.path.exists("tmp"):
                shutil.rmtree("tmp")
