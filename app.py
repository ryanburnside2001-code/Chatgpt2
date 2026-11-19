import os, json, uuid, shutil, subprocess, threading, re
from pathlib import Path
from datetime import datetime, timezone
import httpx
from fastapi import FastAPI, UploadFile, File, HTTPException, Header
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE=Path(__file__).resolve().parent
DATA=BASE/'data'; DATA.mkdir(exist_ok=True)
JOBS=DATA/'jobs.json'; LOCK=threading.Lock()
app=FastAPI(title='Remix Studio API')
app.mount('/static',StaticFiles(directory=BASE/'static'),name='static')

def auth(authorization):
    token=os.getenv('APP_TOKEN','')
    if token and authorization != f'Bearer {token}': raise HTTPException(401,'Invalid app token')

def load():
    try: return json.loads(JOBS.read_text())
    except (FileNotFoundError,json.JSONDecodeError): return {}

def save_job(jid, **values):
    with LOCK:
        jobs=load(); jobs[jid]={**jobs.get(jid,{}),**values}; JOBS.write_text(json.dumps(jobs,indent=2))

@app.get('/')
def index(): return FileResponse(BASE/'static'/'index.html')

@app.get('/api/health')
def health():
    return {'ok':True,'ffmpeg':shutil.which('ffmpeg') is not None,'openrouter_configured':bool(os.getenv('OPENROUTER_API_KEY'))}

@app.get('/api/jobs')
def jobs(authorization: str|None=Header(None)):
    auth(authorization); return list(reversed(list(load().values())))

class ScriptRequest(BaseModel):
    topic:str
    style:str='motivational'
    duration:int=45
    provider:str='openrouter'

async def ai_chat(prompt,provider):
    if provider=='openrouter':
        key=os.getenv('OPENROUTER_API_KEY'); url='https://openrouter.ai/api/v1/chat/completions'
        model=os.getenv('OPENROUTER_MODEL','openai/gpt-4o-mini')
        headers={'Authorization':f'Bearer {key}'}
        payload={'model':model,'messages':[{'role':'user','content':prompt}]}
    elif provider=='openai':
        key=os.getenv('OPENAI_API_KEY'); url='https://api.openai.com/v1/chat/completions'
        headers={'Authorization':f'Bearer {key}'}
        payload={'model':'gpt-4o-mini','messages':[{'role':'user','content':prompt}]}
    elif provider=='anthropic':
        key=os.getenv('ANTHROPIC_API_KEY'); url='https://api.anthropic.com/v1/messages'
        headers={'x-api-key':str(key),'anthropic-version':'2023-06-01'}
        payload={'model':'claude-sonnet-4-5','max_tokens':1200,'messages':[{'role':'user','content':prompt}]}
    elif provider=='gemini':
        key=os.getenv('GEMINI_API_KEY'); model='gemini-2.5-flash'
        url=f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'
        headers={'x-goog-api-key':str(key)}
        payload={'contents':[{'parts':[{'text':prompt}]}]}
    else: raise HTTPException(400,'Unsupported AI provider')
    if not key: raise HTTPException(400,f'{provider} API key not configured in .env')
    async with httpx.AsyncClient(timeout=70) as client:
        try:
            response=await client.post(url,headers=headers,json=payload); response.raise_for_status()
            result=response.json()
            if provider=='anthropic': return result['content'][0]['text']
            if provider=='gemini': return result['candidates'][0]['content']['parts'][0]['text']
            return result['choices'][0]['message']['content']
        except (httpx.HTTPError,KeyError,IndexError) as exc: raise HTTPException(502,f'AI provider error: {str(exc)[:250]}')

@app.post('/api/script')
async def script(req:ScriptRequest,authorization:str|None=Header(None)):
    auth(authorization)
    if not req.topic.strip() or len(req.topic)>1500: raise HTTPException(400,'Topic must be 1-1500 characters')
    prompt=f'Write an ORIGINAL compelling {req.style} short-video voiceover about: {req.topic}. Aim for {max(15,min(req.duration,180))} seconds. Include an opening hook, story and closing call to action. Return just the script. Avoid unsupported guarantees about engagement.'
    return {'script':await ai_chat(prompt,req.provider)}

class ClipRequest(BaseModel):
    job_id:str
    start:float=0
    duration:float=30
    vertical:bool=True

def render(jid,source,start,duration,vertical):
    try:
        output=DATA/f'{jid}.mp4'
        cmd=['ffmpeg','-y','-ss',str(start),'-i',str(source),'-t',str(duration)]
        if vertical:
            cmd+=['-vf','scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1']
        cmd+=['-c:v','libx264','-preset','veryfast','-crf','24','-c:a','aac','-movflags','+faststart',str(output)]
        proc=subprocess.run(cmd,capture_output=True,text=True,timeout=900)
        if proc.returncode: raise RuntimeError(proc.stderr[-1200:])
        save_job(jid,status='completed',output=f'/api/download/{jid}',finished=datetime.now(timezone.utc).isoformat())
    except Exception as exc: save_job(jid,status='failed',error=str(exc)[:1200])

@app.post('/api/upload')
async def upload(file:UploadFile=File(...),authorization:str|None=Header(None)):
    auth(authorization)
    ext=Path(file.filename or '').suffix.lower()
    if ext not in {'.mp4','.mov','.mkv','.webm'}: raise HTTPException(400,'Upload MP4, MOV, MKV or WebM')
    jid=uuid.uuid4().hex; path=DATA/f'{jid}{ext}'; size=0
    with path.open('wb') as dest:
        while chunk:=await file.read(1024*1024):
            size+=len(chunk)
            if size>1024*1024*1024:
                dest.close(); path.unlink(missing_ok=True); raise HTTPException(413,'Max file size is 1 GB')
            dest.write(chunk)
    save_job(jid,id=jid,name=Path(file.filename or 'video').name,status='uploaded',source=str(path),created=datetime.now(timezone.utc).isoformat())
    return {'job_id':jid,'status':'uploaded'}

@app.post('/api/clip')
def clip(req:ClipRequest,authorization:str|None=Header(None)):
    auth(authorization)
    job=load().get(req.job_id)
    if not job or not Path(job.get('source','')).is_file(): raise HTTPException(404,'Upload not found')
    if req.start<0 or not 1<=req.duration<=180: raise HTTPException(400,'Duration must be 1-180 seconds')
    jid=uuid.uuid4().hex
    save_job(jid,id=jid,name=f"Clip of {job['name']}",source=job['source'],status='processing',created=datetime.now(timezone.utc).isoformat())
    threading.Thread(target=render,args=(jid,job['source'],req.start,req.duration,req.vertical),daemon=True).start()
    return {'job_id':jid,'status':'processing'}

@app.get('/api/download/{jid}')
def download(jid:str,authorization:str|None=Header(None)):
    auth(authorization)
    if not re.fullmatch(r'[a-f0-9]{32}',jid): raise HTTPException(400,'Invalid ID')
    job=load().get(jid)
    path=DATA/f'{jid}.mp4'
    if not job or job.get('status')!='completed' or not path.is_file(): raise HTTPException(404,'Not ready')
    return FileResponse(path,media_type='video/mp4',filename=f'remix-{jid[:8]}.mp4')
