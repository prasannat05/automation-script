import requests

url = input("Enter URL: ")
res = requests.get(f"http://tinyurl.com/api-create.php?url={url}")
print("Short URL:", res.text)
