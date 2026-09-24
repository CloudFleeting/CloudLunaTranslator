# Basic Usage  

## HOOK Mode  

For games that are already running, use HOOK mode to open the game process selection window and choose the game's process.  

![img](https://image.lunatranslator.org/zh/basicuse/hook.png)  

After confirming, the game will be added to the software and injected, then the text selection window will pop up. The text selection window can also be opened from the toolbar button.  

![img](https://image.lunatranslator.org/zh/basicuse/select.png)  

Let the game run for a while to display some text. At this point, several candidate text lines will appear in the text selection interface. Choose the line that matches the game's text to start translation.  

![img](https://image.lunatranslator.org/zh/basicuse/show.png)  

If the game supports embedded translation, there will be an "Embed" column of buttons; otherwise, there will only be a "Display" column of buttons.  

![img](https://image.lunatranslator.org/zh/basicuse/embed.png)  

## OCR Mode  

![img](https://image.lunatranslator.org/zh/basicuse/ocrmode.png)

Sometimes, OCR mode can also be used to recognize text from images. Switch to OCR mode, then select the recognition area, and the text will be automatically recognized and translated.  

For separate translations beside each detected text block, open **OCR Settings → Other Settings → Full-screen region translation** and enable **Full-screen detection**. Keep **Keep text regions separate** enabled. **Capture area** can use the selected window, the entire display, or a selected game-content region. For a browser game, first use **Select OCR Region** to outline only the game viewport, then choose **Game content area (selected OCR region)**. This excludes browser tabs, toolbars, and other content outside that region. The **Missing-region grace** and **Text stability** controls help keep translations steady when OCR briefly misses or varies a line.

Please note: Do not use the wrong button. The latter button, which has the same default icon, is only for temporarily selecting and recognizing an image once, not for continuous automatic recognition.  

![img](https://image.lunatranslator.org/zh/basicuse/ocr.png)  

## Quick Launch and HOOK  

After launching the software, drag and drop the game executable into the software window with your mouse. Once released, the game will be automatically added to the software, launched with locale emulation, and hooked automatically.  

![img](https://image.lunatranslator.org/zh/basicuse/load.png)  

The text selection window will then pop up. Proceed with the same steps as in HOOK mode.  

![img](https://image.lunatranslator.org/zh/basicuse/loaded.png)
