@echo off
setlocal
cd /d "%~dp0"

if not exist .git (
  git init
)
git add -A
git -c user.name="muguang1224" -c user.email="muguang1224@users.noreply.github.com" commit -m "matrix-gap v0.1.0"
git tag -a v0.1.0 -m "matrix-gap v0.1.0" 2>nul
git branch -M main

where gh >nul 2>nul
if %ERRORLEVEL%==0 (
  gh repo create matrix-gap --public --description "Audit claimed platform support against the CI matrix that actually tests it." 2>nul
)

git remote remove origin 2>nul
git remote add origin https://github.com/muguang1224/matrix-gap.git
git push -u origin main
git push origin v0.1.0

where gh >nul 2>nul
if %ERRORLEVEL%==0 (
  gh release create v0.1.0 --title "matrix-gap v0.1.0" --notes "Static audit of claimed support vs GitHub Actions coverage. 134 tests; zero dependencies." 2>nul
)

echo.
echo Done. Verify:
echo   https://github.com/muguang1224/matrix-gap
echo   https://github.com/muguang1224/matrix-gap/releases/latest
pause
