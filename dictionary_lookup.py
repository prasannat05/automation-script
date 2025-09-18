import requests
word = input("Enter word: ")
res = requests.get(f"https://api.dictionaryapi.dev/api/v2/entries/en/{word}").json()
print(res[0]['meanings'][0]['definitions'][0]['definition'])
