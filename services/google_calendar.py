"""con funciones que devuelven boludeces, y claramente los parametros son cualquier cosa, sobre todos los tipos no son los que tienen que ser"""

def create_event(user_id : str, titulo : str, start_dt : int, end_dt : int, location=None, note=None):
    return "aca creare el evento"

def delete_event(user_id : str, event_id : str):
    return "aca eliminare evento"

def free_busy(user_id : str, window : float):
    return "aca chequeare disponibilidad"