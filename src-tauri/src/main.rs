// AIPerf 压测可视化 —— 本地离线桌面客户端启动器
//
// 职责：
//   1. 用安装包内置的便携 Python 拉起本地引擎（engine/app.py，仅监听 127.0.0.1 随机端口）
//   2. 轮询 /api/health，就绪后把主窗口从加载页导航到本地引擎页面
//   3. 退出时结束引擎及其拉起的 aiperf 进程树
// 运行期全程离线：tokenizer 使用内置本地目录，不访问 HuggingFace Hub。

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs::{self, File};
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{AppHandle, Manager, RunEvent, Url};

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

struct Engine(Mutex<Option<Child>>);

fn free_port() -> std::io::Result<u16> {
    Ok(TcpListener::bind("127.0.0.1:0")?.local_addr()?.port())
}

fn python_exe(engine: &Path) -> PathBuf {
    if cfg!(windows) {
        engine.join("python").join("python.exe")
    } else {
        engine.join("python").join("bin").join("python3")
    }
}

fn health_ok(port: u16) -> bool {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let Ok(mut s) = TcpStream::connect_timeout(&addr, Duration::from_millis(500)) else {
        return false;
    };
    let _ = s.set_read_timeout(Some(Duration::from_secs(3)));
    if s
        .write_all(b"GET /api/health HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
        .is_err()
    {
        return false;
    }
    let mut buf = Vec::new();
    let _ = s.read_to_end(&mut buf);
    let head = String::from_utf8_lossy(&buf[..buf.len().min(16)]).to_string();
    head.contains(" 200")
}

fn log_tail(path: &Path) -> String {
    let s = fs::read_to_string(path).unwrap_or_default();
    let lines: Vec<&str> = s.lines().collect();
    lines[lines.len().saturating_sub(30)..].join("\n")
}

fn start_engine(app: &AppHandle) -> Result<u16, String> {
    let res = app
        .path()
        .resource_dir()
        .map_err(|e| format!("无法定位资源目录: {e}"))?;
    let engine = dunce::simplified(&res.join("engine")).to_path_buf();
    let py = python_exe(&engine);
    if !py.exists() {
        return Err(format!("内置 Python 不存在: {}", py.display()));
    }

    let data = app
        .path()
        .app_data_dir()
        .map_err(|e| format!("无法定位数据目录: {e}"))?;
    let data = dunce::simplified(&data).to_path_buf();
    fs::create_dir_all(&data).map_err(|e| format!("创建数据目录失败: {e}"))?;

    let port = free_port().map_err(|e| format!("分配本地端口失败: {e}"))?;
    let log_path = data.join("engine.log");
    let log = File::create(&log_path).map_err(|e| format!("创建日志失败: {e}"))?;
    let log2 = log.try_clone().map_err(|e| e.to_string())?;

    let mut cmd = Command::new(&py);
    cmd.arg(engine.join("app.py"))
        .arg("--host")
        .arg("127.0.0.1")
        .arg("--port")
        .arg(port.to_string())
        .env_remove("PYTHONPATH")
        .env_remove("PYTHONHOME")
        .env("PYTHONNOUSERSITE", "1")
        .env("PYTHONUTF8", "1")
        .env("PYTHONIOENCODING", "utf-8")
        .env("AIPERF_WEB_DESKTOP", "1")
        .env("AIPERF_WEB_HOME", &data)
        .env("AIPERF_WEB_STATIC", engine.join("static"))
        .env("AIPERF_PYTHON", &py)
        .env(
            "AIPERF_DEFAULT_TOKENIZER",
            engine.join("tokenizers").join("qwen3.8-27b"),
        )
        // 离线：tokenizer 为本地目录时 aiperf 不访问 Hub。
        // 不能设 HF_HUB_OFFLINE —— aiperf 0.13 在该模式下只认 HF 缓存，会拒绝本地 tokenizer 目录。
        .env_remove("HF_HUB_OFFLINE")
        .env_remove("TRANSFORMERS_OFFLINE")
        .env("HF_HUB_DISABLE_TELEMETRY", "1")
        .env("HF_HOME", data.join("hf-cache"))
        .current_dir(&data)
        .stdin(Stdio::null())
        .stdout(Stdio::from(log))
        .stderr(Stdio::from(log2));
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }

    let child = cmd.spawn().map_err(|e| format!("启动引擎失败: {e}"))?;
    app.state::<Engine>().0.lock().unwrap().replace(child);

    let deadline = Instant::now() + Duration::from_secs(120);
    while Instant::now() < deadline {
        if health_ok(port) {
            return Ok(port);
        }
        if let Some(c) = app.state::<Engine>().0.lock().unwrap().as_mut() {
            if let Ok(Some(status)) = c.try_wait() {
                return Err(format!(
                    "引擎进程已退出（{status}）\n日志：{}\n\n{}",
                    log_path.display(),
                    log_tail(&log_path)
                ));
            }
        }
        std::thread::sleep(Duration::from_millis(300));
    }
    Err(format!(
        "引擎启动超时\n日志：{}\n\n{}",
        log_path.display(),
        log_tail(&log_path)
    ))
}

fn stop_engine(app: &AppHandle) {
    if let Some(mut child) = app.state::<Engine>().0.lock().unwrap().take() {
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            let pid = child.id().to_string();
            let _ = Command::new("taskkill")
                .args(["/PID", pid.as_str(), "/T", "/F"])
                .creation_flags(CREATE_NO_WINDOW)
                .status();
        }
        let _ = child.kill();
        let _ = child.wait();
    }
}

fn main() {
    let app = tauri::Builder::default()
        .manage(Engine(Mutex::new(None)))
        .setup(|app| {
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                let result = start_engine(&handle);
                #[allow(unused_mut)]
                let Some(mut win) = handle.get_webview_window("main") else {
                    return;
                };
                match result {
                    Ok(port) => {
                        let url = Url::parse(&format!("http://127.0.0.1:{port}/")).unwrap();
                        let _ = win.navigate(url);
                    }
                    Err(msg) => {
                        std::thread::sleep(Duration::from_millis(500));
                        let _ = win.eval(&format!("showError({:?})", msg));
                    }
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build AIPerf Desktop");

    app.run(|handle, event| {
        if let RunEvent::Exit = event {
            stop_engine(handle);
        }
    });
}
