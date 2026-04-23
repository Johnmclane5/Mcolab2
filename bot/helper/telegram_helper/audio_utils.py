from .button_build import ButtonMaker

def create_audio_selection_buttons(mid, tracks, selected_indices, is_multi=False):
    buttons = ButtonMaker()
    for track in tracks:
        index = track['index']
        lang = track['lang']
        title = track['title']
        codec = track['codec']

        tick = "✅" if index in selected_indices else "❌"
        # ❌ means it will be REMOVED (selected for removal), or should it be ✅ for selected to KEEP?
        # User said "audio hatane ka", so ✅ means REMOVE this track.

        button_text = f"{tick} {lang} - {title} ({codec})"
        buttons.data_button(button_text, f"audsel {mid} {index}")

    buttons.data_button("Remove All Audio", f"audsel {mid} all")
    if is_multi:
        buttons.data_button("Apply to All Files", f"audsel {mid} applyall")

    buttons.data_button("Done", f"audsel {mid} done")
    buttons.data_button("Cancel", f"audsel {mid} cancel")

    return buttons.build_menu(1)
