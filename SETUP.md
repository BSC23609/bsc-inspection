# BSC Inspection - Deployment Setup

## ONE-TIME SETUP (do this once)

### Step 1: Install Git on your server
- Download: https://git-scm.com/download/win
- Install with default options

### Step 2: Create GitHub repo
1. Go to https://github.com → Sign in
2. Click **+ → New repository**
3. Name: `bsc-inspection-server`
4. **Public**
5. **Do NOT** check "Add README"
6. Click **Create repository**
7. Copy the URL shown (looks like `https://github.com/USERNAME/bsc-inspection-server.git`)

### Step 3: Setup local folder
1. Create folder: `C:\BSC-Deploy\`
2. Copy ALL files from this download into `C:\BSC-Deploy\`
   - `server.js`
   - `package.json`
   - `render.yaml`
   - `.gitignore`
   - `push.bat`
   - `public/` folder (with both HTML files)

3. Open CMD in `C:\BSC-Deploy\` (Shift+Right-click → "Open command window here")

4. Run these commands ONE TIME (replace YOUR-USERNAME):
```cmd
git init
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/bsc-inspection-server.git
git add .
git commit -m "Initial commit"
git push -u origin main
```

It will ask for GitHub login first time → use GitHub username + Personal Access Token (NOT password).

**To create Personal Access Token:**
- GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)
- Generate new token (classic)
- Scopes: check **repo**
- Copy the token and paste when CMD asks for password

### Step 4: Enable GitHub Pages
1. Your repo on GitHub → **Settings** → **Pages** (left sidebar)
2. Source: **Deploy from a branch**
3. Branch: **main** / Folder: **/public**
4. Click **Save**
5. Wait 1 min. Site live at: `https://YOUR-USERNAME.github.io/bsc-inspection-server/bharat-steel-inspection.html`

### Step 5: Setup Render (auto-deploys from GitHub)
1. Go to https://render.com → Sign up with GitHub
2. Dashboard → **+ New** → **Blueprint**
3. Select repo: `bsc-inspection-server`
4. Render auto-reads `render.yaml` and sets everything up
5. Click **Apply**
6. Wait 5 mins. Copy the URL (looks like `https://bsc-inspection-server.onrender.com`)

### Step 6: Update HTML with Render URL
In `C:\BSC-Deploy\public\bharat-steel-inspection.html`:
- Find: `var SERVER_URL = 'https://lydia-gloveless-graig.ngrok-free.dev'`
- Replace with: `var SERVER_URL = 'https://YOUR-RENDER-URL.onrender.com'`

Same change in `C:\BSC-Deploy\public\bsc-dashboard.html`

Then push the update:
- Double-click `push.bat`
- Enter commit message: `update server URL`
- Done!

### Step 7: Keep server awake (UptimeRobot)
1. https://uptimerobot.com → Register FREE
2. **+ New Monitor**
   - Type: HTTP(s)
   - URL: your Render URL
   - Interval: 5 minutes
3. Create

---

## DAILY WORKFLOW

Any time you change HTML or server.js:

1. Edit files in `C:\BSC-Deploy\`
2. Double-click `push.bat`
3. Type a short message (or just press Enter)
4. Done!

- GitHub Pages updates HTML in ~30 seconds
- Render redeploys server in ~2-3 minutes

---

## URLs to bookmark

- **Form:** https://YOUR-USERNAME.github.io/bsc-inspection-server/bharat-steel-inspection.html
- **Dashboard:** https://YOUR-USERNAME.github.io/bsc-inspection-server/bsc-dashboard.html
- **Render dashboard:** https://dashboard.render.com
- **GitHub repo:** https://github.com/YOUR-USERNAME/bsc-inspection-server
