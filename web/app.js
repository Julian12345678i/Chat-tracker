const $ = id => document.getElementById(id);

const statusEl = $("status");
const joinScreen = $("join-screen");
const chatScreen = $("chat-screen");
const joinForm = $("join-form");
const usernameInput = $("username");
const roomInput = $("room");
const usersList = $("users");
const messagesEl = $("messages");
const typingEl = $("typing");
const messageForm = $("message-form");
const messageInput = $("message-input");
const callBar = $("call-bar");
const callStatus = $("call-status");
const muteButton = $("mute-button");
const hangupButton = $("hangup-button");
const incomingBox = $("incoming");
const incomingText = $("incoming-text");
const remoteAudio = $("remote-audio");

const FALLBACK_ICE = [{ urls: "stun:stun.l.google.com:19302" }];

let socket = null;
let username = "";
let roomId = "lobby";
let roomUsers = [];

let pc = null;
let localStream = null;
let currentPeer = null;      // peer we are calling / in a call with
let incoming = null;         // { from, offer } while ringing
let pendingCandidates = [];  // ICE received before we can apply it
let starting = false;        // guards async setup (mic prompt etc.)

const typingUsers = new Set();
let typingTimer = null;
let iAmTyping = false;

// ---------- startup ----------

fetch("/health")
    .then(r => r.json())
    .then(d => { statusEl.textContent = d.status === "online" ? "Server online" : "Server unavailable"; })
    .catch(() => { statusEl.textContent = "Could not connect to server."; });

const params = new URLSearchParams(location.search);
roomInput.value = params.get("room") || "lobby";
try { usernameInput.value = localStorage.getItem("og-chat-username") || ""; } catch (e) {}

joinForm.addEventListener("submit", e => { e.preventDefault(); connect(); });

$("new-room-button").addEventListener("click", async () => {
    try {
        const r = await fetch("/api/rooms", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ private: true })
        });
        const data = await r.json();
        roomInput.value = data.room_id;
        statusEl.textContent = "Private room created. Enter a username and join.";
    } catch (e) {
        statusEl.textContent = "Could not create room.";
    }
});

$("copy-invite").addEventListener("click", async () => {
    const link = `${location.origin}/?room=${encodeURIComponent(roomId)}`;
    try {
        await navigator.clipboard.writeText(link);
        addSystem("Invite link copied.");
    } catch (e) {
        addSystem(`Invite link: ${link}`);
    }
});

$("leave-button").addEventListener("click", leaveRoom);
hangupButton.addEventListener("click", () => closeCall(true, "Call ended."));
muteButton.addEventListener("click", toggleMute);
$("accept-button").addEventListener("click", acceptIncoming);
$("decline-button").addEventListener("click", declineIncoming);
window.addEventListener("beforeunload", () => {
    if (currentPeer) sendSignal("webrtc_hangup", currentPeer);
    if (incoming) sendSignal("call_decline", incoming.from);
});

// ---------- connection ----------

function connect() {
    const name = usernameInput.value.trim();
    if (!name) return;

    username = name;
    roomId = roomInput.value.trim() || "lobby";
    try { localStorage.setItem("og-chat-username", username); } catch (e) {}

    if (socket) { socket.onclose = null; socket.close(); }

    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(
        `${scheme}://${location.host}/ws/${encodeURIComponent(roomId)}?username=${encodeURIComponent(username)}`
    );
    socket = ws;
    statusEl.textContent = "Connecting to room...";

    ws.onmessage = handleMessage;
    ws.onerror = () => { statusEl.textContent = "Connection error"; };
    ws.onclose = ev => {
        if (socket !== ws) return;
        socket = null;
        resetSession();
        const reasons = {
            4404: "Room not found.",
            4409: "That username is already in use in this room.",
            4400: "Invalid username."
        };
        statusEl.textContent = reasons[ev.code] || "Disconnected from chat.";
    };
}

function leaveRoom() {
    if (currentPeer) sendSignal("webrtc_hangup", currentPeer);
    if (socket) { const ws = socket; socket = null; ws.close(); }
    resetSession();
    statusEl.textContent = "Left the room.";
}

function resetSession() {
    closeCall(false);
    clearIncoming();
    roomUsers = [];
    typingUsers.clear();
    messagesEl.innerHTML = "";
    typingEl.textContent = "";
    usersList.innerHTML = "";
    chatScreen.hidden = true;
    joinScreen.hidden = false;
}

function sendSignal(type, to, payload = {}) {
    if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type, to, ...payload }));
    }
}

// ---------- chat ----------

function handleMessage(event) {
    let data;
    try { data = JSON.parse(event.data); } catch (e) { return; }

    switch (data.type) {
        case "welcome":
            username = data.username;
            roomId = data.room;
            joinScreen.hidden = true;
            chatScreen.hidden = false;
            statusEl.textContent = `Connected to ${roomId} as ${username}`;
            renderUsers(data.users || []);
            messageInput.focus();
            break;
        case "message":
            typingUsers.delete(data.from);
            renderTyping();
            addMessage(data.from, data.text);
            break;
        case "system":
            addSystem(data.text);
            refreshUsers();
            break;
        case "typing":
            if (data.typing) typingUsers.add(data.from); else typingUsers.delete(data.from);
            renderTyping();
            break;
        case "webrtc_offer": onOffer(data.from, data.offer); break;
        case "webrtc_answer": onAnswer(data.from, data.answer); break;
        case "webrtc_ice_candidate": onIceCandidate(data.from, data.candidate); break;
        case "webrtc_hangup": onPeerHangup(data.from); break;
        case "call_decline": onPeerDecline(data.from); break;
        case "error": addSystem(data.text || "Error"); break;
    }
}

function addMessage(from, text) {
    const mine = from === username;
    const el = document.createElement("div");
    el.className = "msg" + (mine ? " me" : "");
    if (!mine) {
        const who = document.createElement("span");
        who.className = "who";
        who.textContent = from;
        el.appendChild(who);
    }
    el.appendChild(document.createTextNode(text));
    appendToLog(el);
}

function addSystem(text) {
    const el = document.createElement("div");
    el.className = "sys";
    el.textContent = text;
    appendToLog(el);
}

function appendToLog(el) {
    const atBottom = messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 80;
    messagesEl.appendChild(el);
    if (atBottom || el.classList.contains("me")) messagesEl.scrollTop = messagesEl.scrollHeight;
}

function renderTyping() {
    const names = [...typingUsers];
    typingEl.textContent = names.length
        ? `${names.join(", ")} ${names.length > 1 ? "are" : "is"} typing...`
        : "";
}

messageForm.addEventListener("submit", e => {
    e.preventDefault();
    const text = messageInput.value.trim();
    if (!text || !socket || socket.readyState !== WebSocket.OPEN) return;
    socket.send(JSON.stringify({ type: "message", text }));
    messageInput.value = "";
    setTyping(false);
});

messageInput.addEventListener("input", () => {
    setTyping(true);
    clearTimeout(typingTimer);
    typingTimer = setTimeout(() => setTyping(false), 2000);
});

function setTyping(state) {
    if (state === iAmTyping || !socket || socket.readyState !== WebSocket.OPEN) return;
    iAmTyping = state;
    socket.send(JSON.stringify({ type: "typing", typing: state }));
}

// ---------- user list ----------

async function refreshUsers() {
    try {
        const r = await fetch(`/api/rooms/${encodeURIComponent(roomId)}`);
        if (r.ok) renderUsers((await r.json()).users || []);
    } catch (e) { /* best effort */ }
}

function renderUsers(users) {
    roomUsers = users;
    usersList.innerHTML = "";

    users.filter(u => u !== username).forEach(user => {
        const li = document.createElement("li");
        const name = document.createElement("span");
        name.textContent = user;
        const btn = document.createElement("button");
        btn.textContent = "Call";
        btn.disabled = !!(currentPeer || incoming || starting);
        btn.addEventListener("click", () => startCall(user));
        li.append(name, btn);
        usersList.appendChild(li);
    });

    // End calls/ringing with someone who left the room.
    if (currentPeer && !users.includes(currentPeer)) closeCall(false, "Call ended: user left.");
    if (incoming && !users.includes(incoming.from)) clearIncoming();
}

// ---------- calling (WebRTC: audio over UDP, peer to peer) ----------

async function getIceServers() {
    try {
        const r = await fetch("/api/ice");
        if (r.ok) return (await r.json()).iceServers || FALLBACK_ICE;
    } catch (e) {}
    return FALLBACK_ICE;
}

async function getMic() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        throw new Error("Microphone not available (HTTPS required)");
    }
    return navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
    });
}

async function createPeerConnection(peer) {
    const iceServers = await getIceServers();
    const conn = new RTCPeerConnection({ iceServers });
    pc = conn;
    pendingCandidates = pendingCandidates.filter(c => c.from === peer);

    localStream.getTracks().forEach(t => conn.addTrack(t, localStream));

    conn.onicecandidate = ev => {
        if (ev.candidate) sendSignal("webrtc_ice_candidate", peer, { candidate: ev.candidate.toJSON() });
    };

    conn.ontrack = ev => {
        remoteAudio.srcObject = ev.streams[0] || new MediaStream([ev.track]);
        remoteAudio.play().catch(() => { callStatus.textContent = "Tap the page to hear audio."; });
    };

    conn.onconnectionstatechange = () => {
        if (pc !== conn) return;
        if (conn.connectionState === "connected") {
            callStatus.textContent = `In call with ${peer}`;
        } else if (conn.connectionState === "failed") {
            closeCall(true, "Call failed: could not connect (a TURN server may be needed).");
        } else if (conn.connectionState === "disconnected") {
            setTimeout(() => {
                if (pc === conn && conn.connectionState === "disconnected") closeCall(true, "Call lost.");
            }, 5000);
        }
    };
}

function showCallBar(text) {
    callBar.hidden = false;
    callStatus.textContent = text;
    muteButton.textContent = "Mute";
}

async function startCall(target) {
    if (currentPeer || incoming || starting || !target) return;
    starting = true;
    showCallBar(`Calling ${target}...`);
    renderUsers(roomUsers);

    try {
        localStream = await getMic();
    } catch (e) {
        starting = false;
        closeCall(false, `Microphone error: ${e.message}`);
        return;
    }

    if (!socket) { starting = false; closeCall(false); return; }

    currentPeer = target;
    try {
        await createPeerConnection(target);
        const offer = await pc.createOffer();
        await pc.setLocalDescription(offer);
        sendSignal("webrtc_offer", target, { offer: pc.localDescription });
        callStatus.textContent = `Ringing ${target}...`;
    } catch (e) {
        starting = false;
        closeCall(true, `Could not start call: ${e.message}`);
        return;
    }
    starting = false;
}

function onOffer(from, offer) {
    if (!from || !offer) return;
    if (currentPeer || incoming || starting) {
        sendSignal("call_decline", from);
        return;
    }
    incoming = { from, offer };
    incomingText.textContent = `${from} is calling you...`;
    incomingBox.hidden = false;
    renderUsers(roomUsers);
}

function clearIncoming() {
    incoming = null;
    incomingBox.hidden = true;
    pendingCandidates = pendingCandidates.filter(c => c.from === currentPeer);
}

function declineIncoming() {
    if (!incoming) return;
    sendSignal("call_decline", incoming.from);
    clearIncoming();
    renderUsers(roomUsers);
}

async function acceptIncoming() {
    if (!incoming || starting) return;
    const { from, offer } = incoming;
    incomingBox.hidden = true;
    starting = true;
    showCallBar(`Connecting to ${from}...`);

    try {
        localStream = await getMic();
    } catch (e) {
        starting = false;
        sendSignal("call_decline", from);
        incoming = null;
        closeCall(false, `Microphone error: ${e.message}`);
        return;
    }

    // Caller may have hung up while the mic prompt was open.
    if (!incoming || incoming.from !== from || !socket) {
        starting = false;
        closeCall(false, "Caller hung up.");
        return;
    }

    incoming = null;
    currentPeer = from;

    try {
        await createPeerConnection(from);
        await pc.setRemoteDescription(offer);
        await flushCandidates();
        const answer = await pc.createAnswer();
        await pc.setLocalDescription(answer);
        sendSignal("webrtc_answer", from, { answer: pc.localDescription });
        callStatus.textContent = `In call with ${from}`;
    } catch (e) {
        starting = false;
        closeCall(true, `Could not answer call: ${e.message}`);
        return;
    }
    starting = false;
    renderUsers(roomUsers);
}

async function onAnswer(from, answer) {
    if (!pc || from !== currentPeer || !answer) return;
    try {
        await pc.setRemoteDescription(answer);
        await flushCandidates();
        callStatus.textContent = `In call with ${from}`;
    } catch (e) {
        closeCall(true, `Could not complete call: ${e.message}`);
    }
}

async function onIceCandidate(from, candidate) {
    if (!candidate) return;
    const relevant = from === currentPeer || (incoming && from === incoming.from);
    if (!relevant) return;

    if (pc && from === currentPeer && pc.remoteDescription) {
        try { await pc.addIceCandidate(candidate); } catch (e) { console.warn("Bad ICE candidate", e); }
    } else {
        pendingCandidates.push({ from, candidate });
    }
}

async function flushCandidates() {
    const list = pendingCandidates.filter(c => c.from === currentPeer);
    pendingCandidates = [];
    for (const c of list) {
        try { await pc.addIceCandidate(c.candidate); } catch (e) { console.warn("Bad ICE candidate", e); }
    }
}

function onPeerHangup(from) {
    if (incoming && incoming.from === from) {
        clearIncoming();
        addSystem(`Missed call from ${from}.`);
        renderUsers(roomUsers);
    } else if (from === currentPeer) {
        closeCall(false, `${from} ended the call.`);
    }
}

function onPeerDecline(from) {
    if (from === currentPeer) closeCall(false, `${from} is unavailable or declined.`);
}

function toggleMute() {
    if (!localStream) return;
    const track = localStream.getAudioTracks()[0];
    if (!track) return;
    track.enabled = !track.enabled;
    muteButton.textContent = track.enabled ? "Mute" : "Unmute";
}

function closeCall(notify = false, message = "") {
    const peer = currentPeer;
    if (notify && peer) sendSignal("webrtc_hangup", peer);

    if (pc) {
        const conn = pc;
        pc = null;
        conn.onicecandidate = conn.ontrack = conn.onconnectionstatechange = null;
        conn.close();
    }
    if (localStream) {
        localStream.getTracks().forEach(t => t.stop());
        localStream = null;
    }

    remoteAudio.srcObject = null;
    currentPeer = null;
    starting = false;
    pendingCandidates = incoming ? pendingCandidates.filter(c => c.from === incoming.from) : [];
    callBar.hidden = true;
    if (message && !chatScreen.hidden) addSystem(message);
    renderUsers(roomUsers);
}