# KickDropsMiner
Aplicacion de escritorio en Python para trabajar con streams de Kick usando una interfaz CustomTkinter y automatizacion con Selenium.


He visto por ahi alguno pero este es el mejor funciona os lo garantizo :)
## Requisitos

- Windows 10/11
- Python 3.10 o superior
- Google Chrome instalado

## Instalacion

Desde PowerShell, en la carpeta del proyecto:

```powershell
cd path\to\KickDropsMiner-main
python -m pip install -r requirements.txt
```

## Arranque

La forma mas sencilla en Windows:

```powershell
.\run.bat
```

Tambien se puede lanzar directamente:

```powershell
python main.py
```

## Archivos locales no incluidos en Git

Estos archivos/carpetas contienen datos locales o temporales y no se suben al repositorio:

- `config.json`
- `cookies/`
- `chrome_data/`
- `utils/config.json`
- `utils/cookies/`
- `utils/chrome_data/`
- `*.lock`
- `__pycache__/`

## Dependencias principales

Las dependencias estan en `requirements.txt`:

- `customtkinter`
- `pillow`
- `selenium`
- `webdriver-manager`
- `undetected-chromedriver`
- `browser-cookie3`
