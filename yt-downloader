from pytube import YouTube

def download_video(url, path="."):
    yt = YouTube(url)
    stream = yt.streams.get_highest_resolution()
    stream.download(path)
    print(f" Downloaded: {yt.title}")

if __name__ == "__main__":
    url = input("Enter YouTube URL: ")
    path = input("Enter download path (default .): ") or "."
    download_video(url, path)
