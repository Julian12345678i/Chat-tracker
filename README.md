# TCP Chat (CSC 423)

A multi-user real-time chat app using Python TCP sockets and JSON messages.

## Run locally
1. Start the server: `python server.py`
2. In other terminals, start clients: `python client.py`
   (to connect to another machine: `python client.py <host> <port>`)

## Message format
Newline-delimited JSON, e.g. `{"type": "message", "text": "hello"}`
