/* Blue Marble globe. Longitude/latitude sampling uses the same archive bounds as Leaflet. */
class AtlasGlobe {
  constructor(host) {
    this.host = host;
    this.canvas = host.querySelector('canvas');
    this.pose = { lat: 48, lon: 69.5, scale: 1 };
    this.bounds = [46, 40, 93, 56];
    this.image = new Image();
    this.image.onload = () => {
      try { this.initWebGL(); } catch (err) {
        this.canvas.replaceWith(this.canvas.cloneNode());
        this.canvas = host.querySelector('canvas');
        this.gl = null;
      }
      if (!this.gl) this.initFallback();
      host.dataset.mode = this.gl ? 'webgl' : 'static';
      this.draw();
    };
    // A 2048-px texture keeps the first load small for visitors on a public link.
    const local = /^(localhost|127\.\d+\.\d+\.\d+|\[::1\])$/.test(location.hostname) || location.protocol === 'file:';
    this.image.src = local ? 'assets/blue-marble.jpg' : 'assets/blue-marble-lite.jpg';
    new ResizeObserver(() => this.draw()).observe(host);
    host.addEventListener('pointerdown', event => {
      if (!this.gl || event.button !== 0) return;
      host.setPointerCapture(event.pointerId);
      this.drag = [event.clientX, event.clientY, this.pose.lon, this.pose.lat];
    });
    host.addEventListener('pointermove', event => {
      if (!this.drag) return;
      this.rotate(this.drag[2] - (event.clientX - this.drag[0]) * .15,
                  this.drag[3] + (event.clientY - this.drag[1]) * .13);
    });
    host.addEventListener('lostpointercapture', () => this.drag = null);
    host.addEventListener('keydown', event => {
      const delta = { ArrowLeft: [-3, 0], ArrowRight: [3, 0], ArrowUp: [0, 3], ArrowDown: [0, -3] }[event.key];
      if (!delta || !this.gl) return;
      event.preventDefault();
      this.rotate(this.pose.lon + delta[0], this.pose.lat + delta[1]);
    });
    this.canvas.addEventListener('webglcontextlost', event => {
      event.preventDefault();
      this.canvas.replaceWith(this.canvas.cloneNode());
      this.canvas = host.querySelector('canvas');
      this.gl = null;
      this.initFallback();
      host.dataset.mode = 'static';
      this.draw();
    });
  }

  rotate(lon, lat) {
    this.pose.lon = Math.min(90, Math.max(49, lon));
    this.pose.lat = Math.min(61, Math.max(34, lat));
    this.draw();
  }

  setBounds(bounds) {
    this.bounds = bounds;
    this.draw();
  }

  initWebGL() {
    const gl = this.canvas.getContext('webgl', { alpha: true, premultipliedAlpha: false, antialias: false });
    if (!gl) return;
    const vertex = 'attribute vec2 position; void main(){gl_Position=vec4(position,0.,1.);}';
    const fragment = [
      'precision highp float;',
      'uniform vec2 size, look; uniform float scale; uniform vec4 bounds; uniform sampler2D earth;',
      'const float PI=3.14159265359;',
      'void main(){',
      'float d=min(size.x,size.y); vec2 p=(gl_FragCoord.xy-size*.5)/(d*.43*scale);',
      'float r=length(p);',
      'if(r>=1.036){gl_FragColor=vec4(0.);return;}',
      'if(r>1.){float a=1.-smoothstep(1.,1.036,r); gl_FragColor=vec4(.35,.63,.81,a*.32);return;}',
      'vec3 n=vec3(p,sqrt(max(0.,1.-r*r)));',
      'float cp=cos(look.x), sp=sin(look.x), cl=cos(look.y), sl=sin(look.y);',
      'vec3 v=n.x*vec3(-sl,cl,0.)+n.y*vec3(-sp*cl,-sp*sl,cp)+n.z*vec3(cp*cl,cp*sl,sp);',
      'vec2 geo=vec2(atan(v.y,v.x),asin(clamp(v.z,-1.,1.)))*180./PI;',
      'vec3 base=texture2D(earth,vec2(geo.x/360.+.5,.5-geo.y/180.)).rgb;',
      'float lit=.48+.58*max(0.,dot(n,normalize(vec3(-.46,.38,.8))));',
      'vec3 color=base*lit;',
      'vec2 grid=abs(mod(geo+7.5,15.)-7.5);',
      'float fine=.20/scale/max(n.z,.34);',
      'color=mix(color,vec3(.61,.72,.80),(1.-smoothstep(0.,fine,min(grid.x,grid.y)))*.15*n.z);',
      'bool inside=geo.x>=bounds.x && geo.x<=bounds.z && geo.y>=bounds.y && geo.y<=bounds.w;',
      'float border=min(min(abs(geo.x-bounds.x),abs(geo.x-bounds.z)),min(abs(geo.y-bounds.y),abs(geo.y-bounds.w)));',
      'if(inside)color=mix(color,vec3(.43,.76,.90),.10+.8*(1.-smoothstep(0.,fine*2.,border)));',
      'color=mix(color,vec3(.38,.65,.83),pow(1.-n.z,4.)*.63);',
      'gl_FragColor=vec4(color,1.);}'
    ].join('\n');
    const program = gl.createProgram();
    for (const [type, source] of [[gl.VERTEX_SHADER, vertex], [gl.FRAGMENT_SHADER, fragment]]) {
      const shader = gl.createShader(type);
      gl.shaderSource(shader, source);
      gl.compileShader(shader);
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) throw Error(gl.getShaderInfoLog(shader));
      gl.attachShader(program, shader);
    }
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw Error(gl.getProgramInfoLog(program));
    gl.useProgram(program);
    const quad = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, quad);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1, 1,-1, -1,1, 1,1]), gl.STATIC_DRAW);
    const position = gl.getAttribLocation(program, 'position');
    gl.enableVertexAttribArray(position);
    gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);
    const texture = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    const bitmap = document.createElement('canvas');
    bitmap.width = Math.min(this.image.width, gl.getParameter(gl.MAX_TEXTURE_SIZE));
    bitmap.height = bitmap.width / 2;
    bitmap.getContext('2d').drawImage(this.image, 0, 0, bitmap.width, bitmap.height);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, bitmap);
    this.uniform = Object.fromEntries(['size', 'look', 'scale', 'bounds'].map(name => [name, gl.getUniformLocation(program, name)]));
    this.gl = gl;
  }

  initFallback() {
    const bitmap = document.createElement('canvas');
    bitmap.width = 2048;
    bitmap.height = 1024;
    const context = bitmap.getContext('2d', { willReadFrequently: true });
    context.drawImage(this.image, 0, 0, bitmap.width, bitmap.height);
    this.source = context.getImageData(0, 0, bitmap.width, bitmap.height);
  }

  draw() {
    if (!this.gl && !this.source) return;
    const width = this.host.clientWidth, height = this.host.clientHeight;
    if (!width || !height) return;
    if (this.gl) {
      const gl = this.gl, dpr = Math.min(devicePixelRatio || 1, 1.75), u = this.uniform;
      const pixelWidth = Math.round(width * dpr), pixelHeight = Math.round(height * dpr);
      if (this.canvas.width !== pixelWidth || this.canvas.height !== pixelHeight) {
        this.canvas.width = pixelWidth;
        this.canvas.height = pixelHeight;
      }
      gl.viewport(0, 0, this.canvas.width, this.canvas.height);
      gl.uniform2f(u.size, this.canvas.width, this.canvas.height);
      gl.uniform2f(u.look, this.pose.lat * Math.PI / 180, this.pose.lon * Math.PI / 180);
      gl.uniform1f(u.scale, this.pose.scale);
      gl.uniform4fv(u.bounds, this.bounds);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      return;
    }
    const size = Math.min(620, Math.ceil(Math.min(width, height) * 1.35));
    const key = [size, ...this.bounds].join();
    if (this.fallbackKey === key) return;
    this.fallbackKey = key;
    this.canvas.width = this.canvas.height = size;
    const context = this.canvas.getContext('2d');
    const output = context.createImageData(size, size), source = this.source;
    const lat = 48 * Math.PI / 180, lon = 69.5 * Math.PI / 180;
    const cp = Math.cos(lat), sp = Math.sin(lat), cl = Math.cos(lon), sl = Math.sin(lon);
    const [west, south, east, north] = this.bounds;
    for (let j = 0; j < size; j++) {
      const y = (size / 2 - j) / (size * .43);
      for (let i = 0; i < size; i++) {
        const x = (i - size / 2) / (size * .43), r = Math.hypot(x, y);
        const out = (j * size + i) * 4;
        if (r >= 1) {
          if (r < 1.035) output.data.set([89, 161, 207, 70 * (1.035 - r) / .035], out);
          continue;
        }
        const z = Math.sqrt(1 - r * r);
        const gx = Math.atan2(x * cl - y * sp * sl + z * cp * sl, -x * sl - y * sp * cl + z * cp * cl) * 180 / Math.PI;
        const gy = Math.asin(Math.max(-1, Math.min(1, y * cp + z * sp))) * 180 / Math.PI;
        const px = Math.min(source.width - 1, Math.floor((gx / 360 + .5) * source.width));
        const py = Math.min(source.height - 1, Math.floor((.5 - gy / 180) * source.height));
        const at = (py * source.width + px) * 4;
        const inside = gx >= west && gx <= east && gy >= south && gy <= north;
        const edge = Math.min(gx - west, east - gx, gy - south, north - gy);
        const blend = inside ? (edge < .45 ? .84 : .12) : 0;
        const light = .48 + .57 * Math.max(0, -.46 * x + .38 * y + .8 * z);
        const atmosphere = Math.pow(1 - z, 4) * .63;
        for (let channel = 0; channel < 3; channel++) {
          const tone = [100, 176, 217][channel];
          output.data[out + channel] = (source.data[at + channel] * light * (1 - blend) + tone * blend) * (1 - atmosphere) + tone * atmosphere;
        }
        output.data[out + 3] = 255;
      }
    }
    context.putImageData(output, 0, 0);
  }

  fly(callback, still) {
    if (!this.gl || still) { callback(); return; }
    const initial = { ...this.pose };
    const duration = 620, start = performance.now();
    const frame = now => {
      const p = Math.min(1, (now - start) / duration);
      const eased = p * p * (3 - 2 * p);
      this.pose = {
        lat: initial.lat + (48 - initial.lat) * eased,
        lon: initial.lon + (69.5 - initial.lon) * eased,
        scale: 1 + 2.35 * eased
      };
      this.draw();
      if (p < 1) requestAnimationFrame(frame);
      else callback();
    };
    requestAnimationFrame(frame);
  }

  reset() {
    this.pose = { lat: 48, lon: 69.5, scale: 1 };
    this.draw();
  }
}
