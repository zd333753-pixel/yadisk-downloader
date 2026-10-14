"""Generate the application's simple blue document icon using stdlib only."""
import struct
from pathlib import Path
n=64
pixels=bytearray()
for y in range(n-1,-1,-1):
    for x in range(n):
        color=(0,0,0,0)
        if 10<=x<=53 and 4<=y<=59 and not (x>37 and y<20 and x-37>y-4):color=(50,156,255,255)
        if 37<=x<=53 and 4<=y<=20 and x-37<=y-4:color=(155,208,255,255)
        if (30<=x<=34 and 27<=y<=43) or (36<=y<=46 and abs(x-32)<=46-y and abs(x-32)>=42-y) or (22<=x<=43 and 50<=y<=53):color=(255,255,255,255)
        r,g,b,a=color;pixels.extend((b,g,r,a))
dib=struct.pack('<IiiHHIIiiII',40,n,n*2,1,32,0,len(pixels),0,0,0,0)+pixels+bytes(n*n//8)
header=struct.pack('<HHH',0,1,1)+struct.pack('<BBBBHHII',n,n,0,0,1,32,len(dib),22)
Path('app.ico').write_bytes(header+dib)
