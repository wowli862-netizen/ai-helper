const $ = (s) => document.querySelector(s);

let token = localStorage.getItem("ai_orb_token");
let username = localStorage.getItem("ai_orb_username");
let authMode = "login";

let mediaRecorder = null;
let audioChunks = [];
let audioContext = null;
let analyser = null;
let sourceNode = null;
let animationFrame = null;
let silenceStarted = null;
let manuallyStopped = false;
let currentStream = null;
let busy = false;
let lastLocation = null;

const authScreen = $("#authScreen");
const appScreen = $("#appScreen");
const authForm = $("#authForm");
const authError = $("#authError");
const authButton = $("#authButton");
const orbButton = $("#orbButton");
const statusEl = $("#status");
const chat = $("#chat");
const textInput = $("#textInput");

function setAuthMode(mode) {
    authMode = mode;

    document.querySelectorAll(".tab").forEach(t => {
        t.classList.toggle("active", t.dataset.mode === mode);
    });

    authButton.textContent =
        mode === "login"
            ? "Enter the orb"
            : "Create account";

    authError.textContent = "";
}

document.querySelectorAll(".tab").forEach(t => {
    t.addEventListener("click", () => {
        setAuthMode(t.dataset.mode);
    });
});

function authHeaders() {
    return token
        ? { "Authorization": `Bearer ${token}` }
        : {};
}

async function api(url, options = {}) {
    const headers = {
        ...(options.headers || {}),
        ...authHeaders()
    };

    if (
        options.body &&
        !(options.body instanceof FormData)
    ) {
        headers["Content-Type"] = "application/json";
    }

    const res = await fetch(url, {
        ...options,
        headers
    });

    let data = {};

    try {
        data = await res.json();
    } catch {}

    if (!res.ok) {
        throw new Error(
            data.detail || "Request failed"
        );
    }

    return data;
}

function showApp() {
    authScreen.classList.add("hidden");
    appScreen.classList.remove("hidden");

    $("#welcome").textContent =
        username
            ? `Signed in as ${username}`
            : "";

    loadHistory();
}

function showAuth() {
    appScreen.classList.add("hidden");
    authScreen.classList.remove("hidden");
}

authForm.addEventListener("submit", async (e) => {
    e.preventDefault();

    authError.textContent = "";
    authButton.disabled = true;

    try {
        const data = await api(
            authMode === "login"
                ? "/api/login"
                : "/api/register",
            {
                method: "POST",
                body: JSON.stringify({
                    username: $("#username").value.trim(),
                    password: $("#password").value
                })
            }
        );

        token = data.token;
        username = data.username;

        localStorage.setItem(
            "ai_orb_token",
            token
        );

        localStorage.setItem(
            "ai_orb_username",
            username
        );

        showApp();
        requestLocation();

    } catch (err) {
        authError.textContent = err.message;

    } finally {
        authButton.disabled = false;
    }
});

$("#logoutButton").addEventListener(
    "click",
    async () => {

        try {
            await api(
                "/api/logout",
                {
                    method: "POST",
                    body: "{}"
                }
            );
        } catch {}

        stopRecording(true);

        localStorage.removeItem(
            "ai_orb_token"
        );

        localStorage.removeItem(
            "ai_orb_username"
        );

        token = null;
        username = null;

        chat.innerHTML = "";

        showAuth();
    }
);

function requestLocation() {

    if (!navigator.geolocation) {
        return;
    }

    navigator.geolocation.getCurrentPosition(
        async (pos) => {

            lastLocation = {
                latitude: pos.coords.latitude,
                longitude: pos.coords.longitude
            };

            try {

                await api(
                    "/api/location",
                    {
                        method: "POST",
                        body: JSON.stringify(lastLocation)
                    }
                );

            } catch {}
        },

        () => {

            statusEl.textContent =
                "Location permission not granted — city weather still works.";

        },

        {
            enableHighAccuracy: false,
            timeout: 10000,
            maximumAge: 300000
        }
    );
}

async function loadHistory() {

    try {

        const data =
            await api("/api/history");

        chat.innerHTML = "";

        data.messages.forEach(m => {

            renderMessage(
                m.role,
                m.content,
                m.kind,
                m.metadata
            );

        });

        chat.scrollTop =
            chat.scrollHeight;

    } catch {}
}

function escapeHtml(s) {

    return String(s).replace(
        /[&<>"']/g,
        c => ({
            "&": "&amp;",
            "<": "&lt;",
            ">": "&gt;",
            '"': "&quot;",
            "'": "&#039;"
        }[c])
    );
}

function renderMessage(
    role,
    content,
    kind = "chat",
    metadata = {}
) {

    const article =
        document.createElement("article");

    article.className =
        `message ${role}`;

    const roleEl =
        document.createElement("div");

    roleEl.className =
        "message-role";

    roleEl.textContent =
        role === "user"
            ? "You"
            : "AI Orb";

    const body =
        document.createElement("div");

    body.className =
        "message-body";

    body.textContent =
        content || "";

    /*
     * IMAGE MESSAGE
     */

    if (
        role === "assistant" &&
        kind === "image" &&
        metadata.image
    ) {

        const img =
            document.createElement("img");

        img.className =
            "generated-image";

        img.src =
            metadata.image;

        img.alt =
            "AI generated image";

        body.appendChild(img);
    }

    /*
     * WEATHER MESSAGE
     */

    if (
        role === "assistant" &&
        kind === "weather" &&
        metadata.weather
    ) {

        body.appendChild(
            createWeatherCard(
                metadata.weather
            )
        );
    }

    article.append(
        roleEl,
        body
    );

    chat.appendChild(article);

    return article;
}

function createWeatherCard(w) {

    const card =
        document.createElement("div");

    card.className =
        "weather-card";

    const icon =
        w.icon
            ? `https://openweathermap.org/img/wn/${w.icon}@2x.png`
            : "";

    card.innerHTML = `
        <div class="weather-main">

            <img
                class="weather-icon"
                src="${icon}"
                alt=""
            >

            <div>

                <div class="weather-place">
                    ${escapeHtml(w.city)}
                    ${escapeHtml(w.country || "")}
                </div>

                <div class="weather-desc">
                    ${escapeHtml(w.description)}
                </div>

            </div>

            <div class="weather-temp">
                ${w.temperature}°
            </div>

        </div>

        <div class="weather-grid">

            <div class="weather-stat">
                Feels like
                <b>${w.feels_like}°C</b>
            </div>

            <div class="weather-stat">
                Humidity
                <b>${w.humidity}%</b>
            </div>

            <div class="weather-stat">
                Wind
                <b>${w.wind} m/s</b>
            </div>

        </div>
    `;

    return card;
}

function addUserMessage(text) {

    renderMessage(
        "user",
        text
    );

    chat.scrollTop =
        chat.scrollHeight;
}

function addAssistantMessage(data) {

    const meta = {};

    if (data.type === "image") {
        meta.image =
            data.image;
    }

    if (data.type === "weather") {
        meta.weather =
            data.weather;
    }

    renderMessage(
        "assistant",
        data.text,
        data.type,
        meta
    );

    chat.scrollTop =
        chat.scrollHeight;

    playAudio(
        data.audio,
        data.text
    );
}

function playAudio(
    audioB64,
    text
) {

    /*
     * Groq TTS audio
     */

    if (audioB64) {

        const audio =
            new Audio(
                `data:audio/wav;base64,${audioB64}`
            );

        audio.play().catch(() => {});

        return;
    }

    /*
     * Browser TTS fallback
     */

    if (
        "speechSynthesis" in window &&
        text
    ) {

        speechSynthesis.cancel();

        const u =
            new SpeechSynthesisUtterance(text);

        u.rate = 1.02;

        speechSynthesis.speak(u);
    }
}

async function sendText(text) {

    text = text.trim();

    if (!text || busy) {
        return;
    }

    busy = true;

    statusEl.textContent =
        "Thinking…";

    addUserMessage(text);

    textInput.value = "";

    try {

        const data =
            await api(
                "/api/chat",
                {
                    method: "POST",

                    body: JSON.stringify({
                        message: text,

                        latitude:
                            lastLocation?.latitude ??
                            null,

                        longitude:
                            lastLocation?.longitude ??
                            null
                    })
                }
            );

        addAssistantMessage(data);

        statusEl.textContent =
            "Tap the orb to speak";

    } catch (err) {

        renderMessage(
            "assistant",
            `Sorry — ${err.message}`
        );

        statusEl.textContent =
            "Something went wrong";

    } finally {

        busy = false;
    }
}

/*
 * SEND TEXT MESSAGE
 */

$("#sendButton").addEventListener(
    "click",
    () => {
        sendText(
            textInput.value
        );
    }
);

textInput.addEventListener(
    "keydown",
    e => {

        if (
            e.key === "Enter" &&
            !e.shiftKey
        ) {

            e.preventDefault();

            sendText(
                textInput.value
            );
        }
    }
);

textInput.addEventListener(
    "input",
    () => {

        textInput.style.height =
            "auto";

        textInput.style.height =
            Math.min(
                textInput.scrollHeight,
                130
            ) + "px";
    }
);

/*
 * START MICROPHONE
 */

async function startRecording() {

    if (busy) {
        return;
    }

    if (
        !navigator.mediaDevices?.getUserMedia
    ) {

        statusEl.textContent =
            "Microphone is not supported by this browser.";

        return;
    }

    try {

        currentStream =
            await navigator.mediaDevices.getUserMedia({
                audio: true
            });

        audioContext =
            new (
                window.AudioContext ||
                window.webkitAudioContext
            )();

        sourceNode =
            audioContext.createMediaStreamSource(
                currentStream
            );

        analyser =
            audioContext.createAnalyser();

        analyser.fftSize =
            512;

        sourceNode.connect(
            analyser
        );

        audioChunks = [];

        manuallyStopped = false;

        silenceStarted = null;

        let options = {};

        if (
            MediaRecorder.isTypeSupported(
                "audio/webm;codecs=opus"
            )
        ) {

            options.mimeType =
                "audio/webm;codecs=opus";
        }

        mediaRecorder =
            new MediaRecorder(
                currentStream,
                options
            );

        mediaRecorder.ondataavailable =
            e => {

                if (e.data.size) {
                    audioChunks.push(e.data);
                }
            };

        mediaRecorder.onstop =
            sendRecording;

        mediaRecorder.start(150);

        orbButton.classList.add(
            "listening"
        );

        statusEl.textContent =
            "Listening… tap again or pause speaking";

        monitorAudio();

    } catch (err) {

        statusEl.textContent =
            "Microphone permission was denied.";
    }
}

/*
 * ANALYZE MICROPHONE VOLUME
 */

function monitorAudio() {

    if (
        !analyser ||
        !mediaRecorder ||
        mediaRecorder.state !== "recording"
    ) {

        return;
    }

    const data =
        new Uint8Array(
            analyser.fftSize
        );

    analyser.getByteTimeDomainData(
        data
    );

    let sum = 0;

    for (const v of data) {

        const x =
            (v - 128) / 128;

        sum += x * x;
    }

    const rms =
        Math.sqrt(
            sum / data.length
        );

    /*
     * Orb vibration / scale
     */

    const scale =
        Math.min(
            1.16,
            1 + rms * 1.9
        );

    orbButton
        .querySelector(".orb")
        .style.transform =
            `scale(${scale})`;

    /*
     * Silence detection
     */

    if (rms < 0.018) {

        if (!silenceStarted) {
            silenceStarted =
                performance.now();
        }

        if (
            performance.now() -
            silenceStarted >
            1300
        ) {

            if (
                mediaRecorder.state ===
                "recording"
            ) {

                manuallyStopped = false;

                mediaRecorder.stop();

                return;
            }
        }

    } else {

        silenceStarted = null;
    }

    animationFrame =
        requestAnimationFrame(
            monitorAudio
        );
}

/*
 * STOP RECORDING
 */

function stopRecording(force = false) {

    manuallyStopped = force;

    if (
        mediaRecorder &&
        mediaRecorder.state === "recording"
    ) {

        mediaRecorder.stop();
    }

    cleanupAudio();
}

/*
 * CLEANUP MICROPHONE
 */

function cleanupAudio() {

    cancelAnimationFrame(
        animationFrame
    );

    animationFrame = null;

    orbButton.classList.remove(
        "listening"
    );

    orbButton
        .querySelector(".orb")
        .style.transform =
            "scale(1)";

    if (currentStream) {

        currentStream
            .getTracks()
            .forEach(track => {
                track.stop();
            });
    }

    currentStream = null;

    if (audioContext) {

        audioContext
            .close()
            .catch(() => {});

        audioContext = null;
    }

    analyser = null;
    sourceNode = null;
}

/*
 * SEND AUDIO TO GROQ WHISPER
 */

async function sendRecording() {

    const blob =
        new Blob(
            audioChunks,
            {
                type:
                    mediaRecorder?.mimeType ||
                    "audio/webm"
            }
        );

    cleanupAudio();

    if (blob.size < 1000) {

        statusEl.textContent =
            "I didn't hear anything.";

        return;
    }

    busy = true;

    statusEl.textContent =
        "Transcribing…";

    try {

        const form =
            new FormData();

        form.append(
            "audio",
            blob,
            "recording.webm"
        );

        const result =
            await api(
                "/api/transcribe",
                {
                    method: "POST",
                    body: form
                }
            );

        if (!result.text) {

            statusEl.textContent =
                "No speech detected.";

        } else {

            await sendText(
                result.text
            );
        }

    } catch (err) {

        statusEl.textContent =
            `Transcription failed: ${err.message}`;

        busy = false;
    }
}

/*
 * ORB CLICK
 */

orbButton.addEventListener(
    "click",
    () => {

        if (
            mediaRecorder &&
            mediaRecorder.state === "recording"
        ) {

            /*
             * Second click:
             * manually finish recording.
             */

            manuallyStopped = true;

            mediaRecorder.stop();

        } else {

            /*
             * First click:
             * start microphone.
             */

            startRecording();
        }
    }
);

/*
 * RESTORE SESSION
 */

if (token && username) {

    showApp();

} else {

    showAuth();
      }
