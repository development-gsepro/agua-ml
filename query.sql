SELECT id, "idVBox", "Sucursal", "Medidor", "Formato", "Mercado", "Campo", tipo, "M2", "Fecha", "consumoTotal", hora, "ConsumoHora", tag, categoria, setpoint_agua
	FROM public.consumo_agua
    WHERE "Fecha" > '2026-09-06'
    ORDER BY id, "idVBox", "Sucursal", "Medidor", "Formato", "Fecha", hora;