import requests

def test_search():
    url = "http://127.0.0.1:8000/search"
    payload = {"query": "9788499899619"}
    try:
        print(f"Sending POST request to {url} with payload {payload}...")
        response = requests.post(url, data=payload)
        print(f"Status Code: {response.status_code}")
        if response.status_code == 200:
            if "9788499899619" in response.text or "La Casa del Libro" in response.text:
                 print("SUCCESS: Search returned valid HTML with expected content.")
            else:
                 print("WARNING: Status 200 but content might be missing.")
            print(f"Response snippet: {response.text[:500]}")
        else:
            print(f"FAILURE: Status code {response.status_code}")
            print(response.text)
    except Exception as e:
        print(f"EXCEPTION: {e}")

if __name__ == "__main__":
    test_search()
